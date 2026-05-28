"""
api/health.py — 健康檢查端點

提供 liveness 與 readiness 探針，供負載均衡器 / K8s 使用。
"""

import logging
import time

from fastapi import APIRouter
from sqlalchemy import text

from models.base import get_engine
from schemas._common import OkStatusOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=OkStatusOut)
async def liveness():
    """Liveness probe — 程序存活即回 200。"""
    return {"status": "ok"}


@router.get("/ready")
async def readiness():
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
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "db": "unavailable"},
        )
