"""
Portal - school calendar endpoint
"""

import calendar as cal_module
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_

from models.database import get_session, SchoolEvent
from utils.auth import get_current_user

router = APIRouter()


@router.get("/calendar")
def get_portal_calendar(
    year: int = Query(...),
    month: int = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """取得學校行事曆（教師檢視）"""
    session = get_session()
    try:
        _, last_day = cal_module.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)

        events = session.query(SchoolEvent).filter(
            SchoolEvent.is_active == True,
            SchoolEvent.event_date <= end,
            or_(
                SchoolEvent.end_date >= start,
                (SchoolEvent.end_date.is_(None)) & (SchoolEvent.event_date >= start),
            ),
        ).order_by(SchoolEvent.event_date).all()

        EVENT_TYPE_LABELS_LOCAL = {
            "meeting": "會議",
            "activity": "活動",
            "holiday": "假日",
            "general": "一般",
        }

        return [{
            "id": ev.id,
            "title": ev.title,
            "description": ev.description,
            "event_date": ev.event_date.isoformat(),
            "end_date": ev.end_date.isoformat() if ev.end_date else None,
            "event_type": ev.event_type,
            "event_type_label": EVENT_TYPE_LABELS_LOCAL.get(ev.event_type, ev.event_type),
            "is_all_day": ev.is_all_day,
            "start_time": ev.start_time,
            "end_time": ev.end_time,
            "location": ev.location,
        } for ev in events]
    finally:
        session.close()
