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

logger = logging.getLogger(__name__)


def scheduler_enabled() -> bool:
    return bool(get_settings().scheduler.announcement_publish_scheduler_enabled)


def tick(session, now: datetime, last_dispatched_at: datetime) -> int:
    """掃 publish_at in (last_dispatched_at, now] 且有家長 recipients 的公告，逐筆推播。

    Returns dispatched announcement count.
    Caller 負責持久化 last_dispatched_at = now 給下一次 tick。
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
    if not rows:
        return 0

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

    session.commit()
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

    last_dispatched_at = now_taipei_naive()

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
