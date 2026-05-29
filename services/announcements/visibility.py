"""Announcement visibility helpers — time-window predicate + derived status."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_

from models.database import Announcement


def visibility_time_predicate(now: datetime):
    """SQL filter clause: publish_at 已到 AND 尚未 expires_at。

    NULL 語意：
    - publish_at IS NULL → 立即發佈
    - expires_at IS NULL → 永不過期
    """
    return and_(
        or_(Announcement.publish_at.is_(None), Announcement.publish_at <= now),
        or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
    )


def derive_status(ann: Announcement, now: datetime) -> str:
    """Derived status for admin UI: scheduled / active / expired."""
    if ann.publish_at is not None and ann.publish_at > now:
        return "scheduled"
    if ann.expires_at is not None and ann.expires_at <= now:
        return "expired"
    return "active"
