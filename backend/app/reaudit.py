"""采购指标序列 · 连续异常补审清单与费用补审报告。

业务规则（与费用补审口径保持一致）：

1. 实际观测按**有效日**排序；同一个有效日重复提交时只保留一份有效观测
   （后提交覆盖先提交），被替换的记录进入 ``superseded``，不会参与检测。
2. 异常判定使用 Z 分数绝对值大于阈值（默认 ``1.0``）。
3. 连续异常按自然日相邻判定：两个有效异常日相差恰好 1 天才算连续；
   中间缺少一天即视为断开，不能拼成一组。
4. 只有连续异常天数 ``>= 2`` 的日期组才获得补审资格，孤立异常日不计入报告。
5. 费用报告按补审清单的合格日期关联原采购记录，再用指标聚合计算金额。
6. 报告绑定实际采用的观察批次快照，后续新增观测不会改写既有报告；
   同一报告并发确认只产生一次签发结果。

本模块只依赖标准库和 ``app.models``，所有计算都是确定性的。
"""
from __future__ import annotations

import copy
import math
import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping

from app.models import (
    AggregationMethod,
    MetricDataSource,
    MetricDef,
    MetricPeriod,
    ObservationSource,
    ReportBasis,
)

# Z 分数阈值：绝对值「大于」该值才判为异常。
DEFAULT_ZSCORE_THRESHOLD = 1.0
# 与现有异常检测模型保持一致：少于 4 个观测点不做 Z 分数判定。
MIN_ANOMALY_SAMPLE = 4
# 补审资格的最少连续异常天数。
MIN_CONSECUTIVE_DAYS = 2


# ============================================================
# 口径别名与日期解析
# ============================================================

_SOURCE_ALIASES = {
    "ledger": ObservationSource.LEDGER,
    "日账": ObservationSource.LEDGER,
    "原日账": ObservationSource.LEDGER,
    "领走日账": ObservationSource.LEDGER,
    "correction": ObservationSource.CORRECTION,
    "finance_correction": ObservationSource.CORRECTION,
    "更正": ObservationSource.CORRECTION,
    "财务更正": ObservationSource.CORRECTION,
    "补正": ObservationSource.CORRECTION,
    "补正凭证": ObservationSource.CORRECTION,
}

_BASIS_ALIASES = {
    "taken_ledger": ReportBasis.TAKEN_LEDGER,
    "ledger": ReportBasis.TAKEN_LEDGER,
    "日账": ReportBasis.TAKEN_LEDGER,
    "原日账": ReportBasis.TAKEN_LEDGER,
    "领走日账": ReportBasis.TAKEN_LEDGER,
    "finance_correction": ReportBasis.FINANCE_CORRECTION,
    "correction": ReportBasis.FINANCE_CORRECTION,
    "更正": ReportBasis.FINANCE_CORRECTION,
    "财务更正": ReportBasis.FINANCE_CORRECTION,
    "计入财务更正": ReportBasis.FINANCE_CORRECTION,
}


def parse_day(value: date | datetime | str) -> date:
    """把日期/日期时间/ISO 字符串统一成 ``date``，非法值抛出 ``ValueError``。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("有效日不能为空")
        try:
            return date.fromisoformat(text[:10])
        except ValueError as exc:
            raise ValueError(f"无法解析有效日：{value!r}") from exc
    raise ValueError(f"无法解析有效日：{value!r}")


def coerce_source(value: ObservationSource | str) -> ObservationSource:
    if isinstance(value, ObservationSource):
        return value
    key = str(value or "").strip()
    if key in _SOURCE_ALIASES:
        return _SOURCE_ALIASES[key]
    raise ValueError(f"未知的观测来源：{value!r}")


def coerce_basis(value: ReportBasis | str) -> ReportBasis:
    if isinstance(value, ReportBasis):
        return value
    key = str(value or "").strip()
    if key in _BASIS_ALIASES:
        return _BASIS_ALIASES[key]
    raise ValueError(f"未知的出单依据：{value!r}")


# ============================================================
# 采购观测与观察批次
# ============================================================

@dataclass(frozen=True)
class ProcurementRecord:
    """一条实际采购观测（原日账或更正凭证）。"""

    record_id: str
    day: date
    amount: float
    source: ObservationSource = ObservationSource.LEDGER
    submitted_seq: int = 0
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "day", parse_day(self.day))
        object.__setattr__(self, "amount", float(self.amount))
        object.__setattr__(self, "source", coerce_source(self.source))
        if not math.isfinite(self.amount):
            raise ValueError("金额必须是有限数值")
        if int(self.submitted_seq) < 0:
            raise ValueError("提交序号不能为负数")
        object.__setattr__(self, "submitted_seq", int(self.submitted_seq))

    @property
    def date(self) -> date:
        """有效日的别名，便于报告层按业务口径读取。"""
        return self.day

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any] | "ProcurementRecord") -> "ProcurementRecord":
        if isinstance(row, cls):
            return row
        if not isinstance(row, Mapping):
            raise TypeError("采购观测必须是 ProcurementRecord 或字典")
        raw_day = row.get("day", row.get("date"))
        if raw_day is None:
            raise ValueError("采购观测缺少有效日（day/date）")
        if row.get("amount") is None:
            raise ValueError("采购观测缺少金额（amount）")
        return cls(
            record_id=str(row.get("record_id") or row.get("id") or ""),
            day=parse_day(raw_day),
            amount=float(row["amount"]),
            source=coerce_source(row.get("source", ObservationSource.LEDGER)),
            submitted_seq=int(row.get("submitted_seq", row.get("seq", 0)) or 0),
            note=str(row.get("note", "") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "date": self.day.isoformat(),
            "day": self.day.isoformat(),
            "amount": self.amount,
            "source": self.source.value,
            "submitted_seq": self.submitted_seq,
            "note": self.note,
        }


@dataclass(frozen=True)
class ObservationBatch:
    """报告实际采用的观察批次：每个有效日只有一份有效观测。"""

    batch_id: str
    records: tuple[ProcurementRecord, ...]
    basis: ReportBasis = ReportBasis.TAKEN_LEDGER
    superseded: tuple[ProcurementRecord, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        records = tuple(sorted((r if isinstance(r, ProcurementRecord)
                                else ProcurementRecord.from_mapping(r)
                                for r in self.records), key=lambda r: r.day))
        if len({r.day for r in records}) != len(records):
            raise ValueError("观察批次内同一有效日只能保留一份有效观测")
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "superseded", tuple(
            r if isinstance(r, ProcurementRecord) else ProcurementRecord.from_mapping(r)
            for r in self.superseded
        ))
        object.__setattr__(self, "basis", coerce_basis(self.basis))

    @property
    def days(self) -> tuple[date, ...]:
        return tuple(record.day for record in self.records)

    @property
    def amounts(self) -> tuple[float, ...]:
        return tuple(record.amount for record in self.records)

    @property
    def record_count(self) -> int:
        return len(self.records)

    @property
    def date_range(self) -> tuple[date, date] | None:
        if not self.records:
            return None
        return self.records[0].day, self.records[-1].day

    def record_map(self) -> dict[date, ProcurementRecord]:
        return {record.day: record for record in self.records}

    def records_for(self, days: Iterable[date]) -> tuple[ProcurementRecord, ...]:
        wanted = set(days)
        return tuple(record for record in self.records if record.day in wanted)

    def to_dict(self) -> dict[str, Any]:
        date_range = self.date_range
        return {
            "batch_id": self.batch_id,
            "basis": self.basis.value,
            "note": self.note,
            "record_count": self.record_count,
            "superseded_count": len(self.superseded),
            "date_range": (
                [date_range[0].isoformat(), date_range[1].isoformat()]
                if date_range else None
            ),
            "records": [record.to_dict() for record in self.records],
            "superseded": [record.to_dict() for record in self.superseded],
        }


def _normalise_submissions(
    submissions: Iterable[Mapping[str, Any] | ProcurementRecord],
) -> list[ProcurementRecord]:
    """统一输入并给缺省提交序号的记录补一个稳定的递增序号。"""
    normalised: list[ProcurementRecord] = []
    for index, row in enumerate(submissions):
        record = ProcurementRecord.from_mapping(row)
        if record.submitted_seq <= 0:
            record = replace(record, submitted_seq=index + 1)
        normalised.append(record)
    return normalised


def _select_submissions(
    submissions: list[ProcurementRecord],
    *,
    basis: ReportBasis,
    up_to_seq: int | None,
) -> list[ProcurementRecord]:
    """按出单依据和提交截止序号筛选候选观测。

    按领走日账出单时，财务更正不属于该批次；计入财务更正时，
    更正凭证参与当日有效观测的替换。
    """
    candidates = [
        record for record in submissions
        if up_to_seq is None or record.submitted_seq <= up_to_seq
    ]
    if basis == ReportBasis.TAKEN_LEDGER:
        candidates = [
            record for record in candidates
            if record.source == ObservationSource.LEDGER
        ]
    return candidates


def _dedupe_submissions(
    submissions: list[ProcurementRecord],
) -> tuple[tuple[ProcurementRecord, ...], tuple[ProcurementRecord, ...]]:
    """同一有效日只保留最后提交的一份有效观测，其余进入 superseded。"""
    ordered = sorted(
        enumerate(submissions),
        key=lambda pair: (pair[1].day, pair[1].submitted_seq, pair[0]),
    )
    effective_by_day: dict[date, ProcurementRecord] = {}
    superseded: list[ProcurementRecord] = []
    for _, record in ordered:
        previous = effective_by_day.get(record.day)
        if previous is not None:
            superseded.append(previous)
        effective_by_day[record.day] = record
    effective = tuple(effective_by_day[day] for day in sorted(effective_by_day))
    superseded.sort(key=lambda r: (r.day, r.submitted_seq, r.record_id))
    return effective, tuple(superseded)


def build_observation_batch(
    batch_id: str,
    submissions: Iterable[Mapping[str, Any] | ProcurementRecord],
    *,
    basis: ReportBasis | str = ReportBasis.TAKEN_LEDGER,
    up_to_seq: int | None = None,
    note: str = "",
) -> ObservationBatch:
    """从原始提交构建一个可复算的观察批次。"""
    if not str(batch_id or "").strip():
        raise ValueError("观察批次编号不能为空")
    basis = coerce_basis(basis)
    normalised = _normalise_submissions(submissions)
    selected = _select_submissions(normalised, basis=basis, up_to_seq=up_to_seq)
    effective, superseded = _dedupe_submissions(selected)
    return ObservationBatch(
        batch_id=str(batch_id),
        records=effective,
        basis=basis,
        superseded=superseded,
        note=note,
    )


# ============================================================
# 采购日账：提交、领走与更正
# ============================================================

class ProcurementLedger:
    """采购日账：维护原始提交顺序，支持领走日账后追加财务更正。"""

    def __init__(self, series_id: str = "procurement") -> None:
        self.series_id = series_id
        self._submissions: list[ProcurementRecord] = []
        self._next_seq = 1
        self._taken_seq: int | None = None
        self._lock = threading.RLock()

    def submit(
        self,
        *,
        day: date | datetime | str,
        amount: float,
        record_id: str | None = None,
        source: ObservationSource | str = ObservationSource.LEDGER,
        submitted_seq: int | None = None,
        note: str = "",
    ) -> ProcurementRecord:
        with self._lock:
            seq = int(submitted_seq) if submitted_seq is not None else self._next_seq
            if seq <= 0:
                seq = self._next_seq
            self._next_seq = max(self._next_seq, seq + 1)
            record = ProcurementRecord(
                record_id=record_id or f"{self.series_id}-R{seq:03d}",
                day=parse_day(day),
                amount=amount,
                source=coerce_source(source),
                submitted_seq=seq,
                note=note,
            )
            self._submissions.append(record)
            return record

    def submit_many(
        self,
        rows: Iterable[Mapping[str, Any] | ProcurementRecord],
    ) -> tuple[ProcurementRecord, ...]:
        created: list[ProcurementRecord] = []
        for row in rows:
            record = ProcurementRecord.from_mapping(row)
            created.append(self.submit(
                day=record.day,
                amount=record.amount,
                record_id=record.record_id or None,
                source=record.source,
                submitted_seq=record.submitted_seq or None,
                note=record.note,
            ))
        return tuple(created)

    def correct(
        self,
        *,
        day: date | datetime | str,
        amount: float,
        record_id: str | None = None,
        note: str = "财务更正",
    ) -> ProcurementRecord:
        """复核员领走日账后、签发前登记财务更正。"""
        return self.submit(
            day=day,
            amount=amount,
            record_id=record_id,
            source=ObservationSource.CORRECTION,
            note=note,
        )

    def mark_ledger_taken(self) -> int:
        """记录复核员领走原日账时的提交序号。"""
        with self._lock:
            self._taken_seq = max(
                (r.submitted_seq for r in self._submissions
                 if r.source == ObservationSource.LEDGER),
                default=0,
            )
            return self._taken_seq

    @property
    def ledger_taken_seq(self) -> int | None:
        return self._taken_seq

    def submissions(self) -> tuple[ProcurementRecord, ...]:
        with self._lock:
            return tuple(self._submissions)

    def snapshot(
        self,
        *,
        batch_id: str | None = None,
        basis: ReportBasis | str = ReportBasis.TAKEN_LEDGER,
        up_to_seq: int | None = None,
        note: str = "",
    ) -> ObservationBatch:
        """生成报告采用的观察批次快照。

        选择按领走日账出单且未显式指定截止序号时，会自动冻结在
        ``mark_ledger_taken`` 记录的序号上，领走之后新增的观测不会混入。
        """
        basis = coerce_basis(basis)
        with self._lock:
            submissions = list(self._submissions)
            if (basis == ReportBasis.TAKEN_LEDGER
                    and up_to_seq is None
                    and self._taken_seq is not None):
                up_to_seq = self._taken_seq
        resolved_batch_id = batch_id or (
            f"{self.series_id}-{basis.value}-"
            f"{'all' if up_to_seq is None else up_to_seq}"
        )
        return build_observation_batch(
            resolved_batch_id,
            submissions,
            basis=basis,
            up_to_seq=up_to_seq,
            note=note,
        )


# ============================================================
# 异常检测
# ============================================================

@dataclass(frozen=True)
class AnomalyPoint:
    """一个观测点的异常判定结果。"""

    day: date
    value: float
    z_score: float
    is_anomaly: bool
    record_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "date": self.day.isoformat(),
            "value": self.value,
            "z_score": round(self.z_score, 4),
            "is_anomaly": self.is_anomaly,
        }


def score_observations(
    records: Iterable[ProcurementRecord | Mapping[str, Any]],
    threshold: float = DEFAULT_ZSCORE_THRESHOLD,
) -> tuple[AnomalyPoint, ...]:
    """按有效日顺序计算 Z 分数，``abs(z) > threshold`` 判为异常。"""
    if threshold < 0:
        raise ValueError("Z 分数阈值不能为负数")
    normalised = sorted(
        (ProcurementRecord.from_mapping(record) for record in records),
        key=lambda record: record.day,
    )
    if not normalised:
        return ()
    values = [record.amount for record in normalised]
    n = len(values)
    if n < MIN_ANOMALY_SAMPLE:
        return tuple(
            AnomalyPoint(record.day, record.amount, 0.0, False, record.record_id)
            for record in normalised
        )
    mean = sum(values) / n
    variance = sum((value - mean) ** 2 for value in values) / n
    if variance <= 0:
        return tuple(
            AnomalyPoint(record.day, record.amount, 0.0, False, record.record_id)
            for record in normalised
        )
    std = variance ** 0.5
    points: list[AnomalyPoint] = []
    for record in normalised:
        z_score = (record.amount - mean) / std
        points.append(AnomalyPoint(
            day=record.day,
            value=record.amount,
            z_score=z_score,
            is_anomaly=abs(z_score) > threshold,
            record_id=record.record_id,
        ))
    return tuple(points)


@dataclass(frozen=True)
class AnomalyGroup:
    """一组按自然日相邻的异常观测。"""

    group_index: int
    points: tuple[AnomalyPoint, ...]
    records: tuple[ProcurementRecord, ...]

    @property
    def days(self) -> tuple[date, ...]:
        return tuple(point.day for point in self.points)

    @property
    def size(self) -> int:
        return len(self.points)

    def is_eligible(self, min_consecutive_days: int = MIN_CONSECUTIVE_DAYS) -> bool:
        return self.size >= min_consecutive_days

    @property
    def amount(self) -> float:
        return sum(record.amount for record in self.records)

    def to_dict(self, min_consecutive_days: int = MIN_CONSECUTIVE_DAYS) -> dict[str, Any]:
        return {
            "group_index": self.group_index,
            "dates": [day.isoformat() for day in self.days],
            "days": self.size,
            "eligible": self.is_eligible(min_consecutive_days),
            "amount": self.amount,
            "points": [point.to_dict() for point in self.points],
            "records": [record.to_dict() for record in self.records],
        }


@dataclass(frozen=True)
class ConsecutiveAnomalyAuditList:
    """连续异常补审清单。"""

    list_id: str
    batch: ObservationBatch
    threshold: float
    points: tuple[AnomalyPoint, ...]
    groups: tuple[AnomalyGroup, ...]
    min_consecutive_days: int = MIN_CONSECUTIVE_DAYS

    def __post_init__(self) -> None:
        if self.min_consecutive_days < MIN_CONSECUTIVE_DAYS:
            raise ValueError(
                f"补审资格至少需要 {MIN_CONSECUTIVE_DAYS} 天连续异常"
            )

    @property
    def anomaly_points(self) -> tuple[AnomalyPoint, ...]:
        return tuple(point for point in self.points if point.is_anomaly)

    @property
    def anomaly_days(self) -> tuple[date, ...]:
        return tuple(point.day for point in self.anomaly_points)

    @property
    def eligible_groups(self) -> tuple[AnomalyGroup, ...]:
        return tuple(
            group for group in self.groups
            if group.is_eligible(self.min_consecutive_days)
        )

    @property
    def eligible_days(self) -> tuple[date, ...]:
        return tuple(
            day for group in self.eligible_groups for day in group.days
        )

    @property
    def eligible_records(self) -> tuple[ProcurementRecord, ...]:
        record_map = self.batch.record_map()
        return tuple(record_map[day] for day in self.eligible_days)

    @property
    def reaudit_amount(self) -> float:
        return sum(record.amount for record in self.eligible_records)

    def to_dict(self) -> dict[str, Any]:
        date_range = self.batch.date_range
        return {
            "list_id": self.list_id,
            "batch_id": self.batch.batch_id,
            "basis": self.batch.basis.value,
            "threshold": self.threshold,
            "min_consecutive_days": self.min_consecutive_days,
            "observation_count": self.batch.record_count,
            "date_range": (
                [date_range[0].isoformat(), date_range[1].isoformat()]
                if date_range else None
            ),
            "anomaly_dates": [day.isoformat() for day in self.anomaly_days],
            "anomaly_points": [point.to_dict() for point in self.points],
            "groups": [
                group.to_dict(self.min_consecutive_days) for group in self.groups
            ],
            "eligible_dates": [day.isoformat() for day in self.eligible_days],
            "eligible_groups": [
                group.to_dict(self.min_consecutive_days)
                for group in self.eligible_groups
            ],
            "reaudit_records": [
                record.to_dict() for record in self.eligible_records
            ],
            "reaudit_amount": self.reaudit_amount,
        }


def _group_anomaly_points(
    points: tuple[AnomalyPoint, ...],
    record_map: Mapping[date, ProcurementRecord],
) -> tuple[AnomalyGroup, ...]:
    """把异常点按自然日相邻关系分组；缺一天即另起一组。"""
    groups: list[list[AnomalyPoint]] = []
    current: list[AnomalyPoint] = []
    for point in points:
        if not point.is_anomaly:
            continue
        if current and point.day - current[-1].day == timedelta(days=1):
            current.append(point)
        else:
            if current:
                groups.append(current)
            current = [point]
    if current:
        groups.append(current)
    return tuple(
        AnomalyGroup(
            group_index=index,
            points=tuple(group),
            records=tuple(record_map[point.day] for point in group),
        )
        for index, group in enumerate(groups)
    )


def build_consecutive_anomaly_audit_list(
    batch: ObservationBatch,
    *,
    list_id: str | None = None,
    threshold: float = DEFAULT_ZSCORE_THRESHOLD,
    min_consecutive_days: int = MIN_CONSECUTIVE_DAYS,
) -> ConsecutiveAnomalyAuditList:
    """构建连续异常补审清单：按有效日排序、判定异常并核对日期。"""
    if min_consecutive_days < MIN_CONSECUTIVE_DAYS:
        raise ValueError(
            f"补审资格至少需要 {MIN_CONSECUTIVE_DAYS} 天连续异常"
        )
    points = score_observations(batch.records, threshold)
    groups = _group_anomaly_points(points, batch.record_map())
    return ConsecutiveAnomalyAuditList(
        list_id=list_id or f"AUD-{batch.batch_id}",
        batch=batch,
        threshold=float(threshold),
        points=points,
        groups=groups,
        min_consecutive_days=min_consecutive_days,
    )


# ============================================================
# 费用补审报告
# ============================================================

def procurement_amount_metric(field_name: str = "amount") -> MetricDef:
    """补审金额的默认指标口径：按采购金额求和。"""
    return MetricDef(
        metric_id="M_PROCUREMENT_REAUDIT_AMOUNT",
        metric_name="补审采购金额",
        calculation_formula=f"SUM({field_name})",
        aggregation=AggregationMethod.SUM,
        data_source=MetricDataSource.MYSQL,
        period=MetricPeriod.DAILY,
        owner="finance",
        field_name=field_name,
        table_name="procurement_records",
    )


@dataclass(frozen=True)
class ExpenseReauditReport:
    """费用补审报告：按补审清单选记录，再用指标聚合算金额。"""

    report_id: str
    batch: ObservationBatch
    audit_list: ConsecutiveAnomalyAuditList
    basis: ReportBasis = ReportBasis.TAKEN_LEDGER
    metric: MetricDef = field(default_factory=procurement_amount_metric)

    def __post_init__(self) -> None:
        if not str(self.report_id or "").strip():
            raise ValueError("报告编号不能为空")
        object.__setattr__(self, "basis", coerce_basis(self.basis))
        if self.audit_list.batch.batch_id != self.batch.batch_id:
            raise ValueError("补审清单与报告采用的观察批次不一致")

    def _report_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for record in self.audit_list.eligible_records:
            row = {
                "record_id": record.record_id,
                "amount": record.amount,
                "date": record.day.isoformat(),
                "source": record.source.value,
            }
            if self.metric.field_name not in row:
                row[self.metric.field_name] = record.amount
            rows.append(row)
        return rows

    def compute(self) -> dict[str, Any]:
        """按报告绑定的观察批次重算；结果只取决于该批次快照。"""
        records = self.audit_list.eligible_records
        amount = self.metric.compute(self._report_rows())
        date_range = self.batch.date_range
        return {
            "report_id": self.report_id,
            "basis": self.basis.value,
            "batch_id": self.batch.batch_id,
            "audit_list_id": self.audit_list.list_id,
            "threshold": self.audit_list.threshold,
            "metric_id": self.metric.metric_id,
            "aggregation": self.metric.aggregation.value,
            "observation_count": self.batch.record_count,
            "date_range": (
                [date_range[0].isoformat(), date_range[1].isoformat()]
                if date_range else None
            ),
            "anomaly_dates": [
                day.isoformat() for day in self.audit_list.anomaly_days
            ],
            "eligible_dates": [
                day.isoformat() for day in self.audit_list.eligible_days
            ],
            "consecutive_groups": [
                group.to_dict(self.audit_list.min_consecutive_days)
                for group in self.audit_list.groups
            ],
            "reaudit_records": [record.to_dict() for record in records],
            "record_count": len(records),
            "reaudit_amount": amount,
            "amount": amount,
        }

    def recompute(self) -> dict[str, Any]:
        """按实际采用的观察批次重新计算报表数据。"""
        return self.compute()


@dataclass(frozen=True)
class ReportConfirmation:
    """一次报告签发确认。``created`` 区分首次确认与并发重复确认。"""

    confirmation_id: str
    report_id: str
    data: dict[str, Any]
    created: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "confirmation_id": self.confirmation_id,
            "report_id": self.report_id,
            "created": self.created,
            "data": self.data,
        }


# ============================================================
# 内存注册表：批次、清单、报告与并发确认
# ============================================================

class ReauditRegistry:
    """保存观察批次/补审清单/费用报告，并提供并发安全的确认。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._batches: dict[str, ObservationBatch] = {}
        self._audits: dict[str, ConsecutiveAnomalyAuditList] = {}
        self._reports: dict[str, ExpenseReauditReport] = {}
        self._confirmations: dict[str, ReportConfirmation] = {}
        self._confirmation_seq = 0

    # -- 批次 -------------------------------------------------
    def add_batch(self, batch: ObservationBatch) -> ObservationBatch:
        with self._lock:
            existing = self._batches.get(batch.batch_id)
            if existing is not None and existing != batch:
                raise ValueError(f"观察批次 {batch.batch_id} 已存在且内容不同")
            self._batches[batch.batch_id] = batch
            return batch

    def create_batch(
        self,
        batch_id: str,
        submissions: Iterable[Mapping[str, Any] | ProcurementRecord],
        *,
        basis: ReportBasis | str = ReportBasis.TAKEN_LEDGER,
        up_to_seq: int | None = None,
        note: str = "",
    ) -> ObservationBatch:
        return self.add_batch(build_observation_batch(
            batch_id,
            submissions,
            basis=basis,
            up_to_seq=up_to_seq,
            note=note,
        ))

    def get_batch(self, batch_id: str) -> ObservationBatch:
        with self._lock:
            batch = self._batches.get(batch_id)
        if batch is None:
            raise KeyError(f"观察批次 {batch_id} 不存在")
        return batch

    # -- 清单 -------------------------------------------------
    def build_audit(
        self,
        batch_id: str,
        *,
        list_id: str | None = None,
        threshold: float = DEFAULT_ZSCORE_THRESHOLD,
        min_consecutive_days: int = MIN_CONSECUTIVE_DAYS,
    ) -> ConsecutiveAnomalyAuditList:
        with self._lock:
            batch = self.get_batch(batch_id)
            audit = build_consecutive_anomaly_audit_list(
                batch,
                list_id=list_id,
                threshold=threshold,
                min_consecutive_days=min_consecutive_days,
            )
            existing = self._audits.get(audit.list_id)
            if existing is not None and existing != audit:
                raise ValueError(f"补审清单 {audit.list_id} 已存在且内容不同")
            self._audits[audit.list_id] = audit
            return audit

    def get_audit(self, list_id: str) -> ConsecutiveAnomalyAuditList:
        with self._lock:
            audit = self._audits.get(list_id)
        if audit is None:
            raise KeyError(f"补审清单 {list_id} 不存在")
        return audit

    # -- 报告 -------------------------------------------------
    def build_report(
        self,
        report_id: str,
        *,
        batch_id: str,
        basis: ReportBasis | str | None = None,
        metric: MetricDef | None = None,
        threshold: float = DEFAULT_ZSCORE_THRESHOLD,
        min_consecutive_days: int = MIN_CONSECUTIVE_DAYS,
        audit_list_id: str | None = None,
        list_id: str | None = None,
    ) -> ExpenseReauditReport:
        with self._lock:
            batch = self.get_batch(batch_id)
            if audit_list_id:
                audit = self.get_audit(audit_list_id)
                if audit.batch.batch_id != batch.batch_id:
                    raise ValueError("补审清单与报告采用的观察批次不一致")
            else:
                audit = build_consecutive_anomaly_audit_list(
                    batch,
                    list_id=list_id or f"AUD-{report_id}",
                    threshold=threshold,
                    min_consecutive_days=min_consecutive_days,
                )
                existing_audit = self._audits.get(audit.list_id)
                if existing_audit is not None and existing_audit != audit:
                    raise ValueError(f"补审清单 {audit.list_id} 已存在且内容不同")
                self._audits[audit.list_id] = audit
            report = ExpenseReauditReport(
                report_id=report_id,
                batch=batch,
                audit_list=audit,
                basis=basis if basis is not None else batch.basis,
                metric=metric or procurement_amount_metric(),
            )
            existing_report = self._reports.get(report_id)
            if existing_report is not None and existing_report != report:
                raise ValueError(f"报告 {report_id} 已存在且采用的批次不同")
            self._reports[report_id] = report
            return report

    def get_report(self, report_id: str) -> ExpenseReauditReport:
        with self._lock:
            report = self._reports.get(report_id)
        if report is None:
            raise KeyError(f"报告 {report_id} 不存在")
        return report

    def recompute_report(self, report_id: str) -> dict[str, Any]:
        """按报告实际采用的观察批次重算，不受后续提交影响。"""
        return self.get_report(report_id).recompute()

    # -- 确认 -------------------------------------------------
    def confirm(self, report_id: str) -> ReportConfirmation:
        """并发确认同一报告只产生一次签发结果。"""
        with self._lock:
            existing = self._confirmations.get(report_id)
            if existing is not None:
                return ReportConfirmation(
                    confirmation_id=existing.confirmation_id,
                    report_id=existing.report_id,
                    data=copy.deepcopy(existing.data),
                    created=False,
                )
            report = self._reports.get(report_id)
            if report is None:
                raise KeyError(f"报告 {report_id} 不存在")
            self._confirmation_seq += 1
            confirmation = ReportConfirmation(
                confirmation_id=f"CONF-{self._confirmation_seq:04d}-{report_id}",
                report_id=report_id,
                data=report.compute(),
                created=True,
            )
            self._confirmations[report_id] = confirmation
            return ReportConfirmation(
                confirmation_id=confirmation.confirmation_id,
                report_id=report_id,
                data=copy.deepcopy(confirmation.data),
                created=True,
            )

    def get_confirmation(self, report_id: str) -> ReportConfirmation | None:
        with self._lock:
            confirmation = self._confirmations.get(report_id)
            if confirmation is None:
                return None
            return ReportConfirmation(
                confirmation_id=confirmation.confirmation_id,
                report_id=confirmation.report_id,
                data=copy.deepcopy(confirmation.data),
                created=False,
            )

    def confirmation_count(self) -> int:
        with self._lock:
            return len(self._confirmations)


# ============================================================
# 界面行字段：清单行与报告行共用同一套展示口径
# ============================================================

def audit_row_fields(audit: ConsecutiveAnomalyAuditList) -> dict[str, Any]:
    """把补审清单结果格式化成清单行字段。"""
    return {
        "有效观测数": audit.batch.record_count,
        "异常日期": "、".join(day.isoformat() for day in audit.anomaly_days) or "无",
        "连续异常组": "；".join(
            f"{group.days[0].isoformat()}~{group.days[-1].isoformat()}({group.size}天)"
            for group in audit.eligible_groups
        ) or "无",
        "补审金额": audit.reaudit_amount,
    }


def report_row_fields(data: dict[str, Any]) -> dict[str, Any]:
    """把费用报告结果格式化成报告行字段。"""
    return {
        "出单依据": data["basis"],
        "实际异常日期": "、".join(data["anomaly_dates"]) or "无",
        "补审采购记录": "、".join(
            record["record_id"] for record in data["reaudit_records"]
        ) or "无",
        "补审金额": data["reaudit_amount"],
    }


# ============================================================
# 一站式入口：便于测试和接口复用
# ============================================================

def run_procurement_reaudit(
    submissions: Iterable[Mapping[str, Any] | ProcurementRecord],
    *,
    report_id: str = "RPT-REAUDIT",
    basis: ReportBasis | str = ReportBasis.TAKEN_LEDGER,
    threshold: float = DEFAULT_ZSCORE_THRESHOLD,
    metric: MetricDef | None = None,
    batch_id: str | None = None,
    list_id: str | None = None,
    min_consecutive_days: int = MIN_CONSECUTIVE_DAYS,
) -> dict[str, Any]:
    """跑一次实际检测与费用聚合，返回批次、补审清单和报告数据。"""
    basis = coerce_basis(basis)
    batch = build_observation_batch(
        batch_id or f"BATCH-{report_id}-{basis.value}",
        submissions,
        basis=basis,
    )
    audit = build_consecutive_anomaly_audit_list(
        batch,
        list_id=list_id or f"AUD-{batch.batch_id}",
        threshold=threshold,
        min_consecutive_days=min_consecutive_days,
    )
    report = ExpenseReauditReport(
        report_id=report_id,
        batch=batch,
        audit_list=audit,
        basis=basis,
        metric=metric or procurement_amount_metric(),
    )
    return {
        "batch": batch.to_dict(),
        "audit": audit.to_dict(),
        "report": report.compute(),
    }
