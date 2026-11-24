"""采购指标序列 · 连续异常补审清单与费用补审报告测试。

覆盖：
1. 十天序列第九天孤立异常 -> 补审金额 0
2. 领走日账 vs 计入财务更正 -> 0 / 200
3. 同一日重复提交只保留一份有效观测
4. 中间缺一天不能认作连续
5. 并发确认同一报告只出一次
6. 每份报告按实际采用的观察批次可重算
"""
from __future__ import annotations

import threading
from datetime import date, timedelta

import pytest

from app.models import ObservationSource, ReportBasis
from app.reaudit import (
    DEFAULT_ZSCORE_THRESHOLD,
    ExpenseReauditReport,
    ObservationBatch,
    ProcurementLedger,
    ProcurementRecord,
    ReauditRegistry,
    build_consecutive_anomaly_audit_list,
    build_observation_batch,
    run_procurement_reaudit,
    score_observations,
)


START = date(2026, 9, 1)


def day(offset: int) -> date:
    return START + timedelta(days=offset)


def series(amounts: list[float], *, start: date = START) -> list[dict]:
    return [
        {"record_id": f"R{index + 1:03d}", "day": start + timedelta(days=index),
         "amount": amount}
        for index, amount in enumerate(amounts)
    ]


BASELINE = [10.0] * 8


# ============================================================
# 1. 孤立异常不获得补审资格
# ============================================================

class TestIsolatedAnomaly:
    def test_ninth_day_isolated_reports_zero(self):
        result = run_procurement_reaudit(series(BASELINE + [100.0, 10.0]))
        audit = result["audit"]
        assert audit["anomaly_dates"] == ["2026-09-09"]
        assert audit["groups"][0]["dates"] == ["2026-09-09"]
        assert audit["groups"][0]["eligible"] is False
        assert audit["eligible_dates"] == []
        assert result["report"]["reaudit_amount"] == 0.0
        assert result["report"]["record_count"] == 0

    def test_anomaly_threshold_is_strictly_greater(self):
        points = score_observations(
            series(BASELINE + [100.0, 10.0]), threshold=DEFAULT_ZSCORE_THRESHOLD,
        )
        anomalies = [point.day for point in points if point.is_anomaly]
        assert anomalies == [day(8)]
        assert abs(points[8].z_score) > 1.0

    def test_missing_middle_day_keeps_tail_adjacency(self):
        # 去掉 09-06 后，09-09 与 09-10 仍然相邻，末尾两天照常计入。
        amounts = [10.0] * 8 + [100.0, 100.0]
        rows = series(amounts)
        del rows[5]  # 去掉 09-06
        result = run_procurement_reaudit(rows)
        audit = result["audit"]
        assert audit["anomaly_dates"] == ["2026-09-09", "2026-09-10"]
        assert audit["eligible_dates"] == ["2026-09-09", "2026-09-10"]
        assert result["report"]["reaudit_amount"] == 200.0

    def test_gap_between_anomalies_is_not_consecutive(self):
        # 09-08 与 09-10 都是异常；去掉中间的 09-09 只让两者在列表里相邻，
        # 但有效日仍差两天，不能认作连续。
        amounts = [10.0] * 7 + [100.0, 10.0, 100.0]
        rows = series(amounts)
        del rows[8]  # 去掉 09-09
        result = run_procurement_reaudit(rows)
        audit = result["audit"]
        assert audit["anomaly_dates"] == ["2026-09-08", "2026-09-10"]
        assert [group["dates"] for group in audit["groups"]] == [
            ["2026-09-08"], ["2026-09-10"],
        ]
        assert audit["eligible_dates"] == []
        assert result["report"]["reaudit_amount"] == 0.0

    def test_single_anomaly_group_of_two_is_eligible(self):
        result = run_procurement_reaudit(series(BASELINE + [100.0, 100.0]))
        audit = result["audit"]
        assert audit["eligible_dates"] == ["2026-09-09", "2026-09-10"]
        assert result["report"]["reaudit_amount"] == 200.0
        assert [record["record_id"] for record in result["report"]["reaudit_records"]] == [
            "R009", "R010",
        ]


# ============================================================
# 2. 领走日账 vs 计入财务更正
# ============================================================

class TestCorrectionBasis:
    def _ledger_then_correction(self) -> ProcurementLedger:
        ledger = ProcurementLedger("procurement")
        ledger.submit_many(series(BASELINE + [100.0, 10.0]))
        ledger.mark_ledger_taken()
        ledger.correct(day=day(9), amount=100.0, record_id="C010")
        return ledger

    def test_taken_ledger_excludes_correction(self):
        ledger = self._ledger_then_correction()
        batch = ledger.snapshot(basis=ReportBasis.TAKEN_LEDGER)
        audit = build_consecutive_anomaly_audit_list(batch)
        report = ExpenseReauditReport(
            report_id="RPT-LEDGER", batch=batch, audit_list=audit,
            basis=ReportBasis.TAKEN_LEDGER,
        )
        data = report.compute()
        assert data["anomaly_dates"] == ["2026-09-09"]
        assert data["reaudit_amount"] == 0.0
        assert data["observation_count"] == 10

    def test_finance_correction_makes_last_two_days_consecutive(self):
        ledger = self._ledger_then_correction()
        batch = ledger.snapshot(basis=ReportBasis.FINANCE_CORRECTION)
        audit = build_consecutive_anomaly_audit_list(batch)
        report = ExpenseReauditReport(
            report_id="RPT-CORRECTION", batch=batch, audit_list=audit,
            basis=ReportBasis.FINANCE_CORRECTION,
        )
        data = report.compute()
        assert data["anomaly_dates"] == ["2026-09-09", "2026-09-10"]
        assert data["eligible_dates"] == ["2026-09-09", "2026-09-10"]
        assert data["reaudit_amount"] == 200.0
        assert [r["record_id"] for r in data["reaudit_records"]] == ["R009", "C010"]

    def test_taken_ledger_snapshot_is_frozen(self):
        ledger = self._ledger_then_correction()
        first = ledger.snapshot(basis=ReportBasis.TAKEN_LEDGER)
        ledger.correct(day=day(8), amount=100.0, record_id="C009")
        second = ledger.snapshot(basis=ReportBasis.TAKEN_LEDGER)
        assert first == second
        assert first.record_map()[day(9)].amount == 10.0


# ============================================================
# 3. 同一日重复提交
# ============================================================

class TestDuplicateSubmissions:
    def test_duplicate_day_keeps_one_effective_observation(self):
        rows = series(BASELINE + [100.0, 10.0])
        rows.append({"record_id": "DUP", "day": day(9), "amount": 100.0})
        batch = build_observation_batch("B-DUP", rows)
        assert batch.record_count == 10
        assert [record.record_id for record in batch.superseded] == ["R010"]
        assert batch.record_map()[day(9)].record_id == "DUP"
        assert batch.record_map()[day(9)].amount == 100.0

    def test_duplicate_day_does_not_double_count_amount(self):
        rows = series(BASELINE + [100.0, 100.0])
        rows.append({"record_id": "DUP", "day": day(9), "amount": 100.0})
        result = run_procurement_reaudit(rows)
        assert result["audit"]["observation_count"] == 10
        assert result["report"]["reaudit_amount"] == 200.0
        assert len(result["report"]["reaudit_records"]) == 2

    def test_later_submission_wins(self):
        rows = series(BASELINE + [100.0, 10.0])
        rows.append({
            "record_id": "LATE", "day": day(8), "amount": 10.0, "submitted_seq": 99,
        })
        batch = build_observation_batch("B-LATE", rows)
        # 后提交的 10 元不是异常，第九天不再是异常日。
        assert batch.record_map()[day(8)].record_id == "LATE"

    def test_duplicate_within_batch_rejected(self):
        records = (
            ProcurementRecord("A", day(0), 10.0),
            ProcurementRecord("B", day(0), 20.0),
        )
        with pytest.raises(ValueError):
            ObservationBatch(batch_id="B", records=records)


# ============================================================
# 4. 报告复算与批次绑定
# ============================================================

class TestReportRecompute:
    def test_report_uses_its_own_batch(self):
        registry = ReauditRegistry()
        rows = series(BASELINE + [100.0, 10.0])
        registry.create_batch("B1", rows, basis="taken_ledger")
        report = registry.build_report("RPT1", batch_id="B1")
        before = registry.recompute_report("RPT1")
        # 后续新增观测不影响既有报告
        registry.create_batch("B2", rows + [{"record_id": "X", "day": day(10), "amount": 100.0}])
        after = registry.recompute_report("RPT1")
        assert before == after
        assert after["reaudit_amount"] == 0.0

    def test_report_batch_mismatch_rejected(self):
        registry = ReauditRegistry()
        registry.create_batch("B1", series(BASELINE + [100.0, 100.0]))
        audit = registry.build_audit("B1", list_id="AUD-X")
        registry.create_batch("B2", series(BASELINE + [10.0, 10.0]))
        batch2 = registry.get_batch("B2")
        with pytest.raises(ValueError):
            ExpenseReauditReport(
                report_id="RPT-X", batch=batch2, audit_list=audit,
            )

    def test_threshold_changes_result(self):
        rows = series(BASELINE + [100.0, 10.0])
        low = run_procurement_reaudit(rows, threshold=0.5)
        high = run_procurement_reaudit(rows, threshold=3.0)
        assert low["audit"]["anomaly_dates"] == ["2026-09-09"]
        assert high["audit"]["anomaly_dates"] == []


# ============================================================
# 5. 并发确认
# ============================================================

class TestConcurrentConfirmation:
    def test_concurrent_confirm_only_once(self):
        registry = ReauditRegistry()
        rows = series(BASELINE + [100.0, 100.0])
        registry.create_batch("B1", rows)
        registry.build_report("RPT1", batch_id="B1")
        results: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            confirmation = registry.confirm("RPT1")
            with lock:
                results.append(confirmation.created)

        threads = [threading.Thread(target=worker) for _ in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert results.count(True) == 1
        assert results.count(False) == 31
        assert registry.confirmation_count() == 1

    def test_confirmation_data_matches_report(self):
        registry = ReauditRegistry()
        registry.create_batch("B1", series(BASELINE + [100.0, 100.0]))
        registry.build_report("RPT1", batch_id="B1")
        confirmation = registry.confirm("RPT1")
        assert confirmation.data["reaudit_amount"] == 200.0
        duplicate = registry.confirm("RPT1")
        assert duplicate.confirmation_id == confirmation.confirmation_id


# ============================================================
# 6. 平台服务层
# ============================================================

class TestDomainServices:
    def test_audit_service_recomputes_and_checks_dates(self):
        from app.services import service_for

        service = service_for("procurement_audit")
        entry, missing = service.create_entry({
            "清单编号": "PROC-T1",
            "采购序列": "序列T1",
            "观察批次": "BATCH-T1",
            "观察记录": series(BASELINE + [100.0, 100.0]),
        })
        assert missing == []
        assert entry["有效观测数"] == 10
        assert entry["异常日期"] == "2026-09-09、2026-09-10"
        action_entry, message = service.run_action(entry["id"], "核对日期")
        assert action_entry is not None, message
        assert action_entry["status"] == "已核对"
        assert action_entry["补审金额"] == 200.0

    def test_expense_service_confirms_once(self):
        from app.services import service_for

        service = service_for("expense_report")
        entry, missing = service.create_entry({
            "报告编号": "EXPR-T1",
            "补审清单": "PROC-T1",
            "观察批次": "BATCH-T1",
            "出单依据": "taken_ledger",
            "观察记录": series(BASELINE + [100.0, 10.0]),
        })
        assert missing == []
        assert entry["补审金额"] == 0
        first, message = service.run_action(entry["id"], "确认签发")
        assert first is not None, message
        second, message = service.run_action(entry["id"], "确认签发")
        assert second["status"] == "已签发"
        assert "重复确认" in message
