"""
api/health.py — 健康檢查端點

提供 liveness 與 readiness 探針，供負載均衡器 / K8s 使用。
"""

import logging
import os
import time

from fastapi import APIRouter
from sqlalchemy import text

from models.base import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])

_ENV = os.environ.get("ENV", "development").lower()


@router.get("/live")
async def liveness():
    """Liveness probe — 程序存活即回 200。"""
    return {"status": "ok"}


@router.get("/ready")
async def readiness():
    """Readiness probe — 確認 DB 連線池可用；同時回報 env 供 SRE 檢查（INFO-2）。"""
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
            "env": _ENV,
        }
    except Exception as e:
        logger.error("Readiness check failed: %s", e, exc_info=True)
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "db": "unavailable", "env": _ENV},
        )
