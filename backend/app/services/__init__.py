"""领域服务注册表：通用资源路由按模块 key 分派到独立业务服务。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.metric_def import MetricDefService
from app.services.dimension_def import DimensionDefService
from app.services.olap_query import OlapQueryService
from app.services.segment_builder import SegmentBuilderService
from app.services.ab_experiment import AbExperimentService
from app.services.experiment_result import ExperimentResultService
from app.services.attribution_model import AttributionModelService
from app.services.dashboard_def import DashboardDefService
from app.services.drill_path import DrillPathService
from app.services.cohort_analysis import CohortAnalysisService
from app.services.data_export import DataExportService
from app.services.query_cache import QueryCacheService
from app.services.scheduled_analysis import ScheduledAnalysisService
from app.services.metric_compare import MetricCompareService
from app.services.anomaly_detect import AnomalyDetectService
from app.services.data_snapshot import DataSnapshotService
from app.services.user_segment import UserSegmentService
from app.services.event_track import EventTrackService
from app.services.ranking_board import RankingBoardService
from app.services.funnel_def import FunnelDefService
from app.services.dataset_join import DatasetJoinService
from app.services.report_builder import ReportBuilderService
from app.services.procurement_audit import ProcurementAuditService
from app.services.expense_report import ExpenseReportService

SERVICES: dict[str, BaseModuleService] = {
    "metric_def": MetricDefService(),
    "dimension_def": DimensionDefService(),
    "olap_query": OlapQueryService(),
    "segment_builder": SegmentBuilderService(),
    "ab_experiment": AbExperimentService(),
    "experiment_result": ExperimentResultService(),
    "attribution_model": AttributionModelService(),
    "dashboard_def": DashboardDefService(),
    "drill_path": DrillPathService(),
    "cohort_analysis": CohortAnalysisService(),
    "data_export": DataExportService(),
    "query_cache": QueryCacheService(),
    "scheduled_analysis": ScheduledAnalysisService(),
    "metric_compare": MetricCompareService(),
    "anomaly_detect": AnomalyDetectService(),
    "data_snapshot": DataSnapshotService(),
    "user_segment": UserSegmentService(),
    "event_track": EventTrackService(),
    "ranking_board": RankingBoardService(),
    "funnel_def": FunnelDefService(),
    "dataset_join": DatasetJoinService(),
    "report_builder": ReportBuilderService(),
    "procurement_audit": ProcurementAuditService(),
    "expense_report": ExpenseReportService(),
}


def service_for(module: str) -> BaseModuleService | None:
    return SERVICES.get(module)
