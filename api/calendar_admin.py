"""管理端行事曆跨模組聚合 endpoint。

設計：
- 各 layer 由 `_fetch_<layer>(session, from_, to, current_user) -> list[CalendarFeedItem]` 提供
- 主 endpoint 純編排：驗證 window → 過濾 layers → 收集 → 排序 → 回傳
- 權限濾在每個 fetcher 入口（無權限直接 return []），避免越權外洩
"""

from datetime import date
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from models.base import get_session_dep
from schemas.calendar_admin import CalendarFeedItem, CalendarFeedResponse
from utils.auth import get_current_user
from utils.calendar_colors import ALL_LAYERS

router = APIRouter(tags=["calendar-admin"])

MAX_WINDOW_DAYS = 90

LAYER_FETCHERS: dict[
    str, Callable[[Session, date, date, dict], list[CalendarFeedItem]]
] = {}


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
    return CalendarFeedResponse(**{"from": from_}, to=to, items=items)
