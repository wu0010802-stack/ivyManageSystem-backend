"""
api/health.py — 健康檢查端點

提供 liveness 與 readiness 探針，供負載均衡器 / K8s 使用。
"""

import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from schemas._base import IvyBaseModel
from sqlalchemy import text

from models.base import get_engine, get_session
from models.scheduler_heartbeat import SchedulerHeartbeat
from schemas._common import OkStatusOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=OkStatusOut)
def liveness():
    """Liveness probe — 程序存活即回 200。"""
    return {"status": "ok"}


@router.get("/ready")
def readiness():
    """Readiness probe — 確認 DB 連線池可用。

    資安掃描 2026-05-07 P1：response 不再回傳 env 欄位（無認證端點，env 值
    告訴攻擊者部署環境類型，協助針對性 payload）。SRE 改在啟動 log 一次性
    確認；如需動態查詢可在 /health/live 用 X-Health-Token header 私有揭露。
    """
    start = time.monotonic()
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        return {
            "status": "ok",
            "db": "connected",
            "latency_ms": elapsed_ms,
        }
    except Exception as e:
        logger.error("Readiness check failed: %s", e, exc_info=True)
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "db": "unavailable"},
        )




class SchedulerHealthItem(IvyBaseModel):
    """schedulers_health item shape."""

    name: str
    last_success_at: str | None = None
    lag_seconds: float | None = None
    expected_interval_seconds: int
    consecutive_failures: int


class SchedulersHealthOut(IvyBaseModel):
    """GET /health/schedulers — UptimeRobot 公開 endpoint."""

    status: str
    schedulers: list[SchedulerHealthItem]
    lagging: list[SchedulerHealthItem] | None = None


@router.get("/schedulers", response_model=SchedulersHealthOut)
def schedulers_health():
    """檢查所有 scheduler heartbeat lag。

    無權限端點（UptimeRobot 公開可打）。
    200 = 全綠；503 = 至少一個 scheduler lag > 2 × expected_interval。
    啟動後尚未跑過（last_success_at IS NULL）的視為「未滿足 lag 條件」，回 200。
    """
    now = datetime.now(timezone.utc)
    schedulers: list[dict] = []
    lagging: list[dict] = []
    with get_session() as s:
        rows = s.query(SchedulerHeartbeat).all()
        for row in rows:
            if row.last_success_at is None:
                lag_seconds: float | None = None
                is_lagging = False
            else:
                # SQLite 回 naive datetime；PG 回 tz-aware。統一補 UTC 後再相減。
                last_success = row.last_success_at
                if last_success.tzinfo is None:
                    last_success = last_success.replace(tzinfo=timezone.utc)
                lag_seconds = (now - last_success).total_seconds()
                is_lagging = lag_seconds > 2 * row.expected_interval_seconds
            item = {
                "name": row.scheduler_name,
                "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
                "lag_seconds": lag_seconds,
                "expected_interval_seconds": row.expected_interval_seconds,
                "consecutive_failures": row.consecutive_failures,
            }
            schedulers.append(item)
            if is_lagging:
                lagging.append(item)
    if lagging:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "lagging": lagging, "schedulers": schedulers},
        )
    return {"status": "ok", "schedulers": schedulers}
