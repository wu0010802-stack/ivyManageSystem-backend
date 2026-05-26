"""GET /api/internal/metrics — scheduler 觀測 endpoint。

提供 9 個 scheduler 的 last_success_at / consecutive_failures / rows_processed
等運行指標，供上游監控 scrape（uptime / alerting）。

多 worker 限制：metrics 是 per-process in-memory；同一 endpoint 命中不同 worker
時資料不同。response 含 ``worker_pid`` / ``hostname`` 讓監控端依 worker 聚合
（建議 scrape N 次涵蓋所有 worker，或部署單 worker scheduler 配 N 個 API worker）。

權限：AUDIT_LOGS（與其他運行可視性 endpoint 一致）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.scheduler_observability import (
    get_metrics_snapshot,
    get_worker_identity,
)

router = APIRouter(prefix="/api/internal", tags=["internal-metrics"])


class SchedulerMetric(BaseModel):
    last_success_at: str | None
    last_failure_at: str | None
    consecutive_failures: int
    last_rows_processed: int
    total_rows_processed: int
    total_runs: int
    total_failures: int
    last_error_message: str | None


class SchedulerMetricsResponse(BaseModel):
    worker_pid: int
    hostname: str
    schedulers: dict[str, SchedulerMetric]


@router.get("/metrics", response_model=SchedulerMetricsResponse)
def get_scheduler_metrics(
    _current_user: dict = Depends(require_staff_permission(Permission.AUDIT_LOGS)),
) -> SchedulerMetricsResponse:
    """回傳該 worker 的 scheduler 觀測指標快照。"""
    identity = get_worker_identity()
    snapshot = get_metrics_snapshot()
    return SchedulerMetricsResponse(
        worker_pid=int(identity["worker_pid"]),
        hostname=str(identity["hostname"]),
        schedulers={
            name: SchedulerMetric(
                last_success_at=(
                    stats.last_success_at.isoformat() if stats.last_success_at else None
                ),
                last_failure_at=(
                    stats.last_failure_at.isoformat() if stats.last_failure_at else None
                ),
                consecutive_failures=stats.consecutive_failures,
                last_rows_processed=stats.last_rows_processed,
                total_rows_processed=stats.total_rows_processed,
                total_runs=stats.total_runs,
                total_failures=stats.total_failures,
                last_error_message=stats.last_error_message,
            )
            for name, stats in snapshot.items()
        },
    )
