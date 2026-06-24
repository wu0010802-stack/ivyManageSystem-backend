"""services/announcement_publish_scheduler.py — 排程公告自動推播家長 LINE。

Pattern 對齊 services/leave_quota_expiry_scheduler.py asyncio polling。

- tick 純函式 tick(session, now, last_dispatched_at) 易測
- run_announcement_publish_scheduler(stop_event) 為 wiring 入口
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import and_, exists

from config import get_settings
from models.database import (
    Announcement,
    AnnouncementParentRecipient,
)

# 既有家長推播 helper（不重新定義，遵守 DRY）
from api.announcements import _fire_announcement_push
from utils.scheduler_observability import record_rows, scheduler_iteration
from utils.scheduler_watermark import get_watermark, set_watermark

logger = logging.getLogger(__name__)

# 此排程器的時間游標 name（持久化於 scheduler_watermarks 表）
_WATERMARK_NAME = "announcement_publish"


def scheduler_enabled() -> bool:
    return bool(get_settings().scheduler.announcement_publish_scheduler_enabled)


def _initial_watermark(session) -> datetime:
    """重啟後 seed 用的時間游標：優先讀持久化值，無則 fallback now()。

    fallback now() 確保首次啟動（游標尚未建立）不會把所有歷史排程公告
    重推一遍——只有已持久化的游標才會被回放，補上重啟窗口內漏推的公告。
    """
    from utils.taipei_time import now_taipei_naive

    return get_watermark(session, _WATERMARK_NAME) or now_taipei_naive()


def tick(session, now: datetime, last_dispatched_at: datetime) -> int:
    """掃 publish_at in (last_dispatched_at, now] 且有家長 recipients 的公告，逐筆推播。

    Returns dispatched announcement count.

    本函式在 commit 前把游標推進到 now 並持久化（set_watermark），使「enqueue
    通知」與「游標推進」在同一事務原子落地——崩潰只會整批回滾重做，不會出現
    「已推但游標沒進 → 重啟重推家長 LINE」。空窗口也推進游標並 commit，否則
    重啟 seed 用舊游標會重推其後所有公告。
    """
    parent_exists = exists().where(
        AnnouncementParentRecipient.announcement_id == Announcement.id
    )
    rows = (
        session.query(Announcement)
        .filter(
            and_(
                Announcement.publish_at.isnot(None),
                Announcement.publish_at > last_dispatched_at,
                Announcement.publish_at <= now,
                parent_exists,
            )
        )
        .all()
    )

    for ann in rows:
        recipients = (
            session.query(AnnouncementParentRecipient)
            .filter(AnnouncementParentRecipient.announcement_id == ann.id)
            .all()
        )
        # bug #23：單筆推播失敗不可被吞——若吞掉，下方 set_watermark 仍會把游標
        # 推過這筆公告 → 該公告永久略過、家長永不收到。故讓例外自然冒泡：
        # 整個 tick 連同 set_watermark 一起回滾，游標停在 last_dispatched_at，
        # 下一個 interval 重試整批（搭配 bug #22 由 scheduler_iteration 記為失敗）。
        _fire_announcement_push(
            session,
            ann,
            recipients,
            sender_user_id=None,
        )

    set_watermark(session, _WATERMARK_NAME, now)
    session.commit()
    if rows:
        logger.info(
            "announcement publish tick: dispatched=%d (last=%s now=%s)",
            len(rows),
            last_dispatched_at.isoformat(),
            now.isoformat(),
        )
    return len(rows)


async def run_announcement_publish_scheduler(stop_event: asyncio.Event) -> None:
    """Main loop: 每 check_interval 秒跑一次 tick，持久化 last_dispatched_at。"""
    from models.base import session_scope
    from utils.taipei_time import now_taipei_naive
    from utils.advisory_lock import try_scheduler_lock

    check_interval = get_settings().scheduler.announcement_publish_check_interval
    logger.info("announcement publish scheduler 啟動 (interval=%ss)", check_interval)

    while not stop_event.is_set():
        with scheduler_iteration(
            "announcement_publish",
            expected_interval_seconds=check_interval,
        ):
            now = now_taipei_naive()

            # bug #22：tick 例外不可在此被 try/except 吞掉，否則 scheduler_iteration
            # 會把失敗記成成功（監控全綠卻零推播）。讓例外自然冒泡進
            # scheduler_iteration（與其他 13 個 scheduler 及 offboarding SEC-007 一致），
            # 由它記為失敗、退避上報，loop 於下個 interval 重試整批。
            def _run_publish():
                with session_scope() as session:
                    with try_scheduler_lock(
                        session,
                        scheduler_name="announcement_publish",
                        run_key="singleton",
                    ) as acquired:
                        if not acquired:
                            return None
                        # 每次 tick 都從 DB 讀最新 watermark，而非使用 per-process
                        # local 變數；多 worker 時，未取得鎖的 worker 下輪會看到
                        # 已推進的游標，不會重推停機窗口公告。
                        last_dispatched_at = _initial_watermark(session)
                        return tick(
                            session, now=now, last_dispatched_at=last_dispatched_at
                        )

            # 同步 DB + after_commit LINE fan-out 丟 threadpool，不在 event loop 上跑
            # （fan-out 內 WS 廣播本就以 run_coroutine_threadsafe 投回主 loop）。
            dispatched = await asyncio.to_thread(_run_publish)
            if dispatched is not None:
                record_rows("announcement_publish", dispatched)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
        except asyncio.TimeoutError:
            pass
