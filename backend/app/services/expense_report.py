"""费用补审报告领域服务。

报告依据连续异常补审清单挑出需补审的实际采购记录，再用指标聚合算金额；
签发确认并发安全，同一报告只出一次。
"""
from __future__ import annotations

import threading
from typing import Any

from app.reaudit import (
    ExpenseReauditReport,
    build_consecutive_anomaly_audit_list,
    build_observation_batch,
    procurement_amount_metric,
    report_row_fields,
)
from app.services.base import BaseModuleService
from app.services.procurement_audit import (
    _basis,
    _min_days,
    _threshold,
    load_observations,
    public_row,
)
from app.store import store

# 报告行内保存的确认结果：行 id -> 首次确认数据。
_CONFIRMATIONS: dict[int, dict[str, Any]] = {}
_CONFIRM_LOCK = threading.RLock()


def _build_report(entry: dict[str, Any]) -> ExpenseReauditReport:
    report_id = str(entry.get("报告编号") or f"RPT-{entry.get('id')}")
    batch_id = str(entry.get("观察批次") or f"BATCH-{entry.get('id')}")
    basis = _basis(entry)
    batch = build_observation_batch(
        batch_id, load_observations(entry), basis=basis,
    )
    audit = build_consecutive_anomaly_audit_list(
        batch,
        list_id=str(entry.get("补审清单") or f"AUD-{batch_id}"),
        threshold=_threshold(entry),
        min_consecutive_days=_min_days(entry),
    )
    metric = procurement_amount_metric(str(entry.get("_amount_field") or "amount"))
    return ExpenseReauditReport(
        report_id=report_id,
        batch=batch,
        audit_list=audit,
        basis=basis,
        metric=metric,
    )


class ExpenseReportService(BaseModuleService):
    MODULE = "expense_report"
    ENTITY = "费用报告"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["报告编号", "补审清单", "观察批次", "出单依据", "实际异常日期", "补审采购记录", "补审金额", "报告状态"]
    REQUIRED_FIELDS = ["报告编号", "补审清单", "观察批次"]
    STATUS_ORDER = ["待聚合", "已聚合", "已签发", "已作废"]
    ACTION_RULES = {"重算金额": "已聚合", "确认签发": "已签发", "作废报告": "已作废"}
    NEGATIVE_ACTIONS = []

    def list_entries(self, **kwargs: Any):
        rows, total = super().list_entries(**kwargs)
        return [public_row(row) for row in rows], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(self.MODULE, entry_id)
        return public_row(entry) if entry is not None else None

    def _raw(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        entry["_observations"] = load_observations(values)
        entry["_basis"] = _basis(values)
        entry["_threshold"] = _threshold(values)
        entry["_min_days"] = _min_days(values)
        self._refresh(entry)
        return public_row(entry), []

    def _refresh(self, entry: dict[str, Any]) -> dict[str, Any]:
        report = _build_report(entry)
        data = report.compute()
        entry.update(report_row_fields(data))
        entry["报告状态"] = entry.get("status")
        entry["_report_data"] = data
        return entry

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self._raw(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        if action == "作废报告":
            entry["status"] = "已作废"
            entry["pending"] = False
            entry["报告状态"] = "已作废"
            return public_row(entry), f"{self.ENTITY}已作废"

        if action == "重算金额":
            self._refresh(entry)
            entry["status"] = "已聚合"
            entry["pending"] = True
            entry["报告状态"] = "已聚合"
            return public_row(entry), f"{self.ENTITY}已按实际采用的观察批次{self.OPERATION_TERM}"

        # 确认签发：并发确认同一报告只出一次。
        with _CONFIRM_LOCK:
            if entry_id in _CONFIRMATIONS:
                entry["status"] = "已签发"
                entry["pending"] = False
                entry["报告状态"] = "已签发"
                return public_row(entry), "报告已签发，本次为重复确认，未重复出单"
            self._refresh(entry)
            _CONFIRMATIONS[entry_id] = dict(entry["_report_data"])
            entry["status"] = "已签发"
            entry["pending"] = False
            entry["报告状态"] = "已签发"
            return public_row(entry), f"{self.ENTITY}已确认签发"

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        reports = [_build_report(row).compute() for row in store.rows(self.MODULE)]
        snapshot.update({
            "reports": len(reports),
            "issued": len(_CONFIRMATIONS),
            "reaudit_amount": sum(float(data["reaudit_amount"]) for data in reports),
        })
        return snapshot

    def detail(self, entry_id: int) -> dict[str, Any] | None:
        entry = self._raw(entry_id)
        if entry is None:
            return None
        self._refresh(entry)
        return {
            **entry["_report_data"],
            "confirmation": _CONFIRMATIONS.get(entry_id),
        }
