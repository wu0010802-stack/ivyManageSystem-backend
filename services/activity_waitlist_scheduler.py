"""才藝候補名單過期掃描排程。

- 仿 salary_snapshot_scheduler 的 in-process asyncio loop pattern
- 每 ACTIVITY_WAITLIST_CHECK_INTERVAL 秒呼叫 sweep_expired_pending_promotions
- sweep 本身 idempotent 且使用 SELECT FOR UPDATE SKIP LOCKED；多 worker 同啟也安全
- 失敗 log warning，下次 tick 再嘗試（不中斷 loop）
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from models.base import session_scope
from utils.advisory_lock import try_scheduler_lock

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = int(os.getenv("ACTIVITY_WAITLIST_CHECK_INTERVAL", "300"))


def scheduler_enabled() -> bool:
    return os.getenv("ACTIVITY_WAITLIST_SCHEDULER_ENABLED", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _get_activity_service() -> Any:
    """延遲 import 避免循環依賴。"""
    from services.activity_service import activity_service  # noqa: PLC0415

    return activity_service


_LOCK_BUCKET_SECONDS = 300


def _current_lock_bucket() -> str:
    """以 5 分鐘窗口聚合 multi-worker 競爭：同 window 只有一個 worker 拿到鎖。"""
    return str(int(time.time() // _LOCK_BUCKET_SECONDS))


def check_and_sweep_once() -> dict:
    """單次 tick：呼叫 sweep_expired_pending_promotions。回傳結果 dict。

    多 worker 部署時以 advisory lock 避免同窗口內重複發 LINE 通知（雖然 sweep
    本身用 SELECT FOR UPDATE SKIP LOCKED row-safe，仍可能兩 worker 各 sweep
    一半並重複觸發 LINE message）。取不到鎖回傳 ``{"skipped": True}``，外層
    log if 檢查 expired/reminded 自然 falsy 不噪。
    """
    svc = _get_activity_service()
    with session_scope() as session:
        with try_scheduler_lock(
            session,
            scheduler_name="activity_waitlist_sweep",
            run_key=_current_lock_bucket(),
        ) as acquired:
            if not acquired:
                return {"skipped": True}
            result = svc.sweep_expired_pending_promotions(session)
    return result


async def run_activity_waitlist_scheduler(stop_event: asyncio.Event) -> None:
    """每 CHECK_INTERVAL_SECONDS 巡檢一次；失敗 log 不中斷。"""
    logger.info(
        "activity waitlist scheduler started (interval=%ds)",
        CHECK_INTERVAL_SECONDS,
    )
    while not stop_event.is_set():
        try:
            result = check_and_sweep_once()
            if (
                result.get("expired")
                or result.get("reminded")
                or result.get("final_reminded")
            ):
                logger.info(
                    "activity waitlist scheduler tick: expired=%s reminded=%s final_reminded=%s",
                    result.get("expired", 0),
                    result.get("reminded", 0),
                    result.get("final_reminded", 0),
                )
        except Exception:
            logger.exception("activity waitlist scheduler tick failed; continuing")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
