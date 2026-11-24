"""连续异常补审清单领域服务。

清单行保存原始观察提交（``_observations``），每次重算都从有效日重新排序、
去重、检测和分组，保证报出的异常日期与补审金额可复算。
"""
from __future__ import annotations

import json
from typing import Any

from app.models import ReportBasis
from app.reaudit import (
    DEFAULT_ZSCORE_THRESHOLD,
    MIN_CONSECUTIVE_DAYS,
    audit_row_fields,
    build_consecutive_anomaly_audit_list,
    build_observation_batch,
)
from app.services.base import BaseModuleService
from app.store import store


def load_observations(values: dict[str, Any]) -> list[dict[str, Any]]:
    """从登记值或既有行里读取观察记录，支持 JSON 字符串和列表。"""
    raw = values.get("_observations")
    if raw is None:
        raw = values.get("观察记录")
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        raw = json.loads(raw)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("观察记录必须是 JSON 数组")
    return [dict(item) for item in raw]


def _basis(values: dict[str, Any]) -> ReportBasis:
    raw = values.get("_basis") or values.get("出单依据") or ReportBasis.TAKEN_LEDGER.value
    return raw if raw in {b.value for b in ReportBasis} else ReportBasis.TAKEN_LEDGER.value


def _threshold(values: dict[str, Any]) -> float:
    raw = values.get("_threshold", values.get("Z分数阈值", DEFAULT_ZSCORE_THRESHOLD))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_ZSCORE_THRESHOLD


def _min_days(values: dict[str, Any]) -> int:
    raw = values.get("_min_days", MIN_CONSECUTIVE_DAYS)
    try:
        return max(MIN_CONSECUTIVE_DAYS, int(raw))
    except (TypeError, ValueError):
        return MIN_CONSECUTIVE_DAYS


def compute_audit(values: dict[str, Any], *, list_id: str, batch_id: str):
    """按清单行保存的观察批次构建补审清单。"""
    batch = build_observation_batch(
        batch_id,
        load_observations(values),
        basis=_basis(values),
    )
    return build_consecutive_anomaly_audit_list(
        batch,
        list_id=list_id,
        threshold=_threshold(values),
        min_consecutive_days=_min_days(values),
    )


def public_row(entry: dict[str, Any]) -> dict[str, Any]:
    """隐藏内部观察记录等私有字段。"""
    return {k: v for k, v in entry.items() if not k.startswith("_")}


class ProcurementAuditService(BaseModuleService):
    MODULE = "procurement_audit"
    ENTITY = "补审清单"
    OPERATION_TERM = "检测"
    LIST_FIELDS = ["清单编号", "采购序列", "观察批次", "有效观测数", "异常日期", "连续异常组", "补审金额", "清单状态"]
    REQUIRED_FIELDS = ["清单编号", "采购序列", "观察批次"]
    STATUS_ORDER = ["待检测", "已检测", "已核对", "已归档"]
    ACTION_RULES = {"重算清单": "已检测", "核对日期": "已核对", "归档清单": "已归档"}
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
        # 观察记录不是列表字段，登记后挂到行上供重算。
        entry["_observations"] = load_observations(values)
        entry["_basis"] = _basis(values)
        entry["_threshold"] = _threshold(values)
        entry["_min_days"] = _min_days(values)
        self._refresh(entry)
        return public_row(entry), []

    def _refresh(self, entry: dict[str, Any]) -> dict[str, Any]:
        """重算并把异常日期、连续异常组和补审金额写回清单行。"""
        audit = compute_audit(
            entry,
            list_id=str(entry.get("清单编号") or f"AUD-{entry.get('id')}"),
            batch_id=str(entry.get("观察批次") or f"BATCH-{entry.get('id')}"),
        )
        entry.update(audit_row_fields(audit))
        entry["清单状态"] = entry.get("status")
        entry["_audit"] = audit.to_dict()
        return entry

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self._raw(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"
        if action == "归档清单":
            entry["status"] = "已归档"
            entry["pending"] = False
            entry["清单状态"] = "已归档"
            return public_row(entry), f"{self.ENTITY}已归档"

        self._refresh(entry)
        if action == "重算清单":
            entry["status"] = "已检测"
        elif action == "核对日期":
            # 核对异常日期必须都落在实际观测的有效日上。
            audit = entry["_audit"]
            observed = {
                point["date"] for point in audit["anomaly_points"]
                if point["is_anomaly"]
            }
            if observed != set(audit["anomaly_dates"]):
                return None, "异常日期与实际观测的有效日不一致，核对失败"
            entry["status"] = "已核对"
        entry["pending"] = entry["status"] != self.STATUS_ORDER[-1]
        entry["清单状态"] = entry["status"]
        return public_row(entry), f"{self.ENTITY}已{self.OPERATION_TERM if action == '重算清单' else action}"

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        audits = [
            compute_audit(
                row,
                list_id=str(row.get("清单编号") or f"AUD-{row.get('id')}"),
                batch_id=str(row.get("观察批次") or f"BATCH-{row.get('id')}"),
            )
            for row in store.rows(self.MODULE)
        ]
        snapshot.update({
            "audit_lists": len(audits),
            "eligible_groups": sum(len(audit.eligible_groups) for audit in audits),
            "reaudit_amount": sum(audit.reaudit_amount for audit in audits),
        })
        return snapshot

    def detail(self, entry_id: int) -> dict[str, Any] | None:
        entry = self._raw(entry_id)
        if entry is None:
            return None
        self._refresh(entry)
        return entry["_audit"]
