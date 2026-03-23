"""Portal - school calendar endpoint."""

from fastapi import APIRouter, Depends, Query

from models.database import get_session
from services.official_calendar import build_admin_calendar_feed
from utils.auth import get_current_user

router = APIRouter()


@router.get("/calendar")
def get_portal_calendar(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    current_user: dict = Depends(get_current_user),
):
    """取得學校行事曆（與後台同步的教師檢視）。"""
    session = get_session()
    try:
        feed = build_admin_calendar_feed(session, year, month)
        return {
            "year": year,
            "month": month,
            "events": feed["events"],
            "official_sync": feed["official_sync"],
        }
    finally:
        session.close()
