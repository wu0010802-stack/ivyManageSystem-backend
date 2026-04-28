"""
services/security_gc_scheduler.py — 安全支援表 GC 排程

兩個 GC：
- rate_limit_buckets：每 5 分鐘清除超過 1 小時的舊視窗
- jwt_blocklist：每 24 小時清除已過期的黑名單項目

環境變數：
- SECURITY_GC_DISABLED=1 → 完全關閉本排程（用於測試或多 worker 部署時只在主 worker 跑）

設計選擇：使用單一 asyncio.Task 配 stop_event，與 graduation_scheduler 相同模式。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_RATE_LIMIT_GC_INTERVAL_SEC = 5 * 60
_JWT_BLOCKLIST_GC_INTERVAL_SEC = 24 * 60 * 60


def scheduler_enabled() -> bool:
    return os.environ.get("SECURITY_GC_DISABLED", "").lower() not in (
        "1",
        "true",
        "yes",
    )


async def run_security_gc_scheduler(stop_event: asyncio.Event) -> None:
    """主迴圈：定期執行兩個 GC。

    迴圈以 60 秒為基本心跳，分別追蹤兩個 GC 的下次執行時間。
    """
    last_rate_gc = 0.0
    last_jwt_gc = 0.0
    logger.info("security_gc_scheduler started")
    try:
        while not stop_event.is_set():
            now = asyncio.get_event_loop().time()
            if now - last_rate_gc >= _RATE_LIMIT_GC_INTERVAL_SEC:
                _run_rate_limit_gc()
                last_rate_gc = now
            if now - last_jwt_gc >= _JWT_BLOCKLIST_GC_INTERVAL_SEC:
                _run_jwt_blocklist_gc()
                last_jwt_gc = now
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                continue
    finally:
        logger.info("security_gc_scheduler stopped")


def _run_rate_limit_gc() -> None:
    try:
        from utils.rate_limit import cleanup_rate_limit_buckets

        deleted = cleanup_rate_limit_buckets(retention_minutes=60)
        logger.info("rate_limit_buckets GC: 已刪除 %s 列", deleted)
    except Exception as e:
        logger.warning("rate_limit GC 發生例外: %s", e)


def _run_jwt_blocklist_gc() -> None:
    try:
        from utils.auth import cleanup_jwt_blocklist

        deleted = cleanup_jwt_blocklist()
        logger.info(
            "jwt_blocklist GC at %s: 已刪除 %s 列",
            datetime.now(timezone.utc).isoformat(),
            deleted,
        )
    except Exception as e:
        logger.warning("jwt_blocklist GC 發生例外: %s", e)
