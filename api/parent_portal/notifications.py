"""api/parent_portal/notifications.py — 家長端通知偏好（Phase 6）

家長可關閉/挑選 LINE 推播類型。稀疏 row：缺 row 視為 enabled。

端點：
- GET /api/parent/notifications/preferences          回 6 個 event_type 的 enabled
- PUT /api/parent/notifications/preferences          batch upsert
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from models.database import ParentNotificationPreference, get_session
from models.parent_notification import (
    PARENT_NOTIFICATION_CHANNELS,
    PARENT_NOTIFICATION_EVENT_TYPES,
)
from utils.auth import require_parent_role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["parent-notifications"])


class PreferenceUpdate(BaseModel):
    prefs: Dict[str, bool] = Field(
        ...,
        description="event_type → enabled 對映；未列入的 event_type 不變動",
    )


def _all_event_types_default(prefs_rows: list) -> dict:
    """把已存在的 row 套到預設全 True 的字典上。"""
    out = {ev: True for ev in PARENT_NOTIFICATION_EVENT_TYPES}
    for r in prefs_rows:
        if r.event_type in out and r.channel == "line":
            out[r.event_type] = bool(r.enabled)
    return out


@router.get("/preferences")
def get_preferences(current_user: dict = Depends(require_parent_role())):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        rows = (
            session.query(ParentNotificationPreference)
            .filter(
                ParentNotificationPreference.user_id == user_id,
                ParentNotificationPreference.channel == "line",
            )
            .all()
        )
        return {"channel": "line", "prefs": _all_event_types_default(rows)}
    finally:
        session.close()


@router.put("/preferences")
def update_preferences(
    payload: PreferenceUpdate,
    request: Request,
    current_user: dict = Depends(require_parent_role()),
):
    """整批 upsert（缺的 event_type 不動，存在的覆寫）。"""
    user_id = current_user["user_id"]
    # 拒絕未知 event_type，避免 typo 進 DB
    unknown = [
        ev for ev in payload.prefs.keys() if ev not in PARENT_NOTIFICATION_EVENT_TYPES
    ]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的 event_type：{unknown}；可選值：{list(PARENT_NOTIFICATION_EVENT_TYPES)}",
        )

    session = get_session()
    try:
        for ev, enabled in payload.prefs.items():
            existing = (
                session.query(ParentNotificationPreference)
                .filter(
                    ParentNotificationPreference.user_id == user_id,
                    ParentNotificationPreference.event_type == ev,
                    ParentNotificationPreference.channel == "line",
                )
                .first()
            )
            if existing:
                existing.enabled = bool(enabled)
                existing.updated_at = datetime.now()
            else:
                session.add(
                    ParentNotificationPreference(
                        user_id=user_id,
                        event_type=ev,
                        channel="line",
                        enabled=bool(enabled),
                    )
                )
        session.commit()

        request.state.audit_entity_id = str(user_id)
        request.state.audit_summary = f"家長更新通知偏好：{payload.prefs}"

        rows = (
            session.query(ParentNotificationPreference)
            .filter(
                ParentNotificationPreference.user_id == user_id,
                ParentNotificationPreference.channel == "line",
            )
            .all()
        )
        return {"channel": "line", "prefs": _all_event_types_default(rows)}
    finally:
        session.close()


# ── Service helper（給 line_service.should_push_to_parent 用） ──────────────


def is_pref_enabled(
    session,
    *,
    user_id: int,
    event_type: str,
    channel: str = "line",
) -> bool:
    """row 缺 = True；存在則看 enabled 欄。

    Service-layer helper；line_service Phase 6 接通呼叫此函式。
    """
    if channel not in PARENT_NOTIFICATION_CHANNELS:
        return True
    row = (
        session.query(ParentNotificationPreference)
        .filter(
            ParentNotificationPreference.user_id == user_id,
            ParentNotificationPreference.event_type == event_type,
            ParentNotificationPreference.channel == channel,
        )
        .first()
    )
    if row is None:
        return True
    return bool(row.enabled)
