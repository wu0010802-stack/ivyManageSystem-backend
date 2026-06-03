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
from utils.scheduler_observability import scheduler_iteration
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
        try:
            _fire_announcement_push(
                session,
                ann,
                recipients,
                sender_user_id=None,
            )
        except Exception:
            logger.exception(
                "announcement publish scheduler 對 announcement_id=%s push 失敗",
                ann.id,
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

    check_interval = get_settings().scheduler.announcement_publish_check_interval
    logger.info("announcement publish scheduler 啟動 (interval=%ss)", check_interval)

    # 從持久化游標 seed（而非 now()）：重啟後回放，補上停機窗口內漏推的公告
    with session_scope() as session:
        last_dispatched_at = _initial_watermark(session)

    while not stop_event.is_set():
        with scheduler_iteration(
            "announcement_publish",
            expected_interval_seconds=check_interval,
        ):
            now = now_taipei_naive()
            try:
                with session_scope() as session:
                    tick(session, now=now, last_dispatched_at=last_dispatched_at)
                last_dispatched_at = now
            except Exception:
                logger.exception("announcement publish scheduler tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
        except asyncio.TimeoutError:
            pass
