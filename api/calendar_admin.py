"""管理端行事曆跨模組聚合 endpoint。

設計：
- 各 layer 由 `_fetch_<layer>(session, from_, to, current_user) -> list[CalendarFeedItem]` 提供
- 主 endpoint 純編排：驗證 window → 過濾 layers → 收集 → 排序 → 回傳
- 權限濾在每個 fetcher 入口（無權限直接 return []），避免越權外洩
"""

from datetime import date
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from models.base import get_session_dep
from models.event import SchoolEvent
from schemas.calendar_admin import CalendarFeedItem, CalendarFeedResponse
from utils.auth import get_current_user
from utils.calendar_colors import ALL_LAYERS, LAYER_COLORS
from utils.permissions import Permission, has_permission

router = APIRouter(tags=["calendar-admin"])

MAX_WINDOW_DAYS = 90

LAYER_FETCHERS: dict[
    str, Callable[[Session, date, date, dict], list[CalendarFeedItem]]
] = {}


def _fetch_event(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    if not has_permission(current_user.get("permissions", 0), Permission.CALENDAR):
        return []
    stmt = (
        select(
            SchoolEvent.id,
            SchoolEvent.title,
            SchoolEvent.event_date,
            SchoolEvent.end_date,
            SchoolEvent.requires_acknowledgment,
            SchoolEvent.event_type,
        )
        .where(SchoolEvent.is_active.is_(True))
        .where(SchoolEvent.event_date <= to)
        .where(
            or_(
                SchoolEvent.end_date.is_(None) & (SchoolEvent.event_date >= from_),
                SchoolEvent.end_date.is_not(None) & (SchoolEvent.end_date >= from_),
            )
        )
    )
    out: list[CalendarFeedItem] = []
    for r in session.execute(stmt).all():
        color = (
            LAYER_COLORS["event"]["ack"]
            if r.requires_acknowledgment
            else LAYER_COLORS["event"]["default"]
        )
        out.append(
            CalendarFeedItem(
                layer="event",
                id=r.id,
                title=r.title,
                start=r.event_date,
                end=r.end_date or r.event_date,
                all_day=True,
                color=color,
                link=f"/calendar?eventId={r.id}",
                meta={
                    "event_type": r.event_type,
                    "requires_acknowledgment": r.requires_acknowledgment,
                },
            )
        )
    return out


LAYER_FETCHERS["event"] = _fetch_event


@router.get("/admin_feed", response_model=CalendarFeedResponse)
def get_admin_feed(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    layers: str | None = Query(None),
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(get_current_user),
) -> CalendarFeedResponse:
    if to < from_:
        raise HTTPException(status_code=422, detail="to must be >= from")
    if (to - from_).days > MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=422, detail=f"window exceeds {MAX_WINDOW_DAYS} days"
        )

    if layers is None:
        requested = ALL_LAYERS
    else:
        requested = {x.strip() for x in layers.split(",") if x.strip()} & ALL_LAYERS

    items: list[CalendarFeedItem] = []
    for layer in requested:
        fetcher = LAYER_FETCHERS.get(layer)
        if fetcher is None:
            continue
        items.extend(fetcher(session, from_, to, current_user))

    items.sort(key=lambda x: (x.start, x.layer, str(x.id)))
    return CalendarFeedResponse(from_=from_, to=to, items=items)
