"""
services/security_gc_scheduler.py — 安全支援表 GC 排程

兩個 GC：
- rate_limit_buckets：每 5 分鐘清除超過 1 小時的舊視窗
- jwt_blocklist：每 6 小時清除已過期的黑名單項目（資安掃描 2026-05-07 P1，
  原 24 小時太長，高峰登出量會讓 blocklist 表持續長到下次 GC 才縮）

環境變數：
- SECURITY_GC_DISABLED=1 → 完全關閉本排程（用於測試或多 worker 部署時只在主 worker 跑）

設計選擇：使用單一 asyncio.Task 配 stop_event，與 graduation_scheduler 相同模式。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from config import get_settings
from models.base import session_scope
from utils.advisory_lock import try_scheduler_lock
from utils.scheduler_observability import record_rows, scheduler_iteration

logger = logging.getLogger(__name__)

_RATE_LIMIT_GC_INTERVAL_SEC = 5 * 60
# 資安掃描 2026-05-07 P1：原 24h 太長，改 6h；blocklist 內容輕（只 jti+exp）多跑無壓力。
_JWT_BLOCKLIST_GC_INTERVAL_SEC = 6 * 60 * 60
# 招生地址 cache 90d retention（個資法 §19）— 每 24h 跑一次
_RECRUITMENT_GEOCODE_CACHE_GC_INTERVAL_SEC = 24 * 60 * 60
_RECRUITMENT_GEOCODE_CACHE_RETENTION_DAYS = 90


def scheduler_enabled() -> bool:
    return not bool(get_settings().scheduler.security_gc_disabled)


async def run_security_gc_scheduler(stop_event: asyncio.Event) -> None:
    """主迴圈：定期執行兩個 GC。

    迴圈以 60 秒為基本心跳，分別追蹤兩個 GC 的下次執行時間。
    """
    last_rate_gc = 0.0
    last_jwt_gc = 0.0
    last_geocode_gc = 0.0
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
            if now - last_geocode_gc >= _RECRUITMENT_GEOCODE_CACHE_GC_INTERVAL_SEC:
                _run_recruitment_geocode_cache_gc()
                last_geocode_gc = now
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                continue
    finally:
        logger.info("security_gc_scheduler stopped")


def _run_rate_limit_gc() -> None:
    # 多 worker 部署時以 advisory lock（5 分鐘窗口 bucket）避免兩 worker 同時 DELETE
    # 相同列；cleanup_rate_limit_buckets 內部用自己的 connection，advisory lock 是
    # namespace mutex 不會干擾。
    with scheduler_iteration("security_rate_limit_gc"):
        from utils.rate_limit import cleanup_rate_limit_buckets

        with session_scope() as lock_session:
            with try_scheduler_lock(
                lock_session,
                scheduler_name="security_rate_limit_gc",
                run_key=str(int(time.time() // _RATE_LIMIT_GC_INTERVAL_SEC)),
            ) as acquired:
                if not acquired:
                    return
                deleted = cleanup_rate_limit_buckets(retention_minutes=60)
                record_rows("security_rate_limit_gc", int(deleted or 0))
                logger.info("rate_limit_buckets GC: 已刪除 %s 列", deleted)


def _run_jwt_blocklist_gc() -> None:
    # 多 worker 部署時以 advisory lock（6 小時窗口 bucket）互斥；cleanup_jwt_blocklist
    # 內部用自己的 connection，advisory lock 不阻擋實際 DELETE。
    with scheduler_iteration("security_jwt_blocklist_gc"):
        from utils.auth import cleanup_jwt_blocklist

        with session_scope() as lock_session:
            with try_scheduler_lock(
                lock_session,
                scheduler_name="security_jwt_blocklist_gc",
                run_key=str(int(time.time() // _JWT_BLOCKLIST_GC_INTERVAL_SEC)),
            ) as acquired:
                if not acquired:
                    return
                deleted = cleanup_jwt_blocklist()
                record_rows("security_jwt_blocklist_gc", int(deleted or 0))
                logger.info(
                    "jwt_blocklist GC at %s: 已刪除 %s 列",
                    datetime.now(timezone.utc).isoformat(),
                    deleted,
                )


def _gc_recruitment_geocode_cache(session) -> int:
    """純函式：刪除 90 天前已 resolved 的 RecruitmentGeocodeCache row。

    NULL resolved_at（pending / failed）保留不刪。
    回傳刪除 row 數。
    """
    from datetime import timedelta

    from models.recruitment import RecruitmentGeocodeCache

    cutoff = datetime.utcnow() - timedelta(days=_RECRUITMENT_GEOCODE_CACHE_RETENTION_DAYS)
    deleted = session.query(RecruitmentGeocodeCache).filter(
        RecruitmentGeocodeCache.resolved_at.isnot(None),
        RecruitmentGeocodeCache.resolved_at < cutoff,
    ).delete(synchronize_session=False)
    return int(deleted or 0)


def _run_recruitment_geocode_cache_gc() -> None:
    """Scheduler 包裝：advisory lock + observability。"""
    with scheduler_iteration("security_recruitment_geocode_cache_gc"):
        with session_scope() as lock_session:
            with try_scheduler_lock(
                lock_session,
                scheduler_name="security_recruitment_geocode_cache_gc",
                run_key=str(int(time.time() // _RECRUITMENT_GEOCODE_CACHE_GC_INTERVAL_SEC)),
            ) as acquired:
                if not acquired:
                    return
                with session_scope() as session:
                    deleted = _gc_recruitment_geocode_cache(session)
                    record_rows("security_recruitment_geocode_cache_gc", deleted)
                    logger.info(
                        "recruitment_geocode_cache GC: 已刪除 %s 列 (retention=%sd)",
                        deleted,
                        _RECRUITMENT_GEOCODE_CACHE_RETENTION_DAYS,
                    )
