"""
Approval summary router - pending counts for dashboard
"""

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import get_session, LeaveRecord, OvertimeRecord, SchoolEvent
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["approvals"])


_EVENT_TYPE_LABELS = {
    "meeting": "會議",
    "activity": "活動",
    "holiday": "假日",
    "general": "一般",
}


@router.get("/upcoming-events")
def get_upcoming_events(
    days: int = Query(7, ge=1, le=30),
    current_user: dict = Depends(require_permission(Permission.DASHBOARD)),
):
    """取得近期行事曆事件（供儀表板使用）"""
    session = get_session()
    try:
        today = date.today()
        end_date = today + timedelta(days=days)

        events = session.query(SchoolEvent).filter(
            SchoolEvent.is_active == True,
            SchoolEvent.event_date >= today,
            SchoolEvent.event_date <= end_date,
        ).order_by(SchoolEvent.event_date).all()

        return [
            {
                "id": ev.id,
                "title": ev.title,
                "event_date": ev.event_date.isoformat(),
                "end_date": ev.end_date.isoformat() if ev.end_date else None,
                "event_type": ev.event_type,
                "event_type_label": _EVENT_TYPE_LABELS.get(ev.event_type, ev.event_type),
                "location": ev.location,
                "start_time": ev.start_time,
                "end_time": ev.end_time,
                "is_all_day": ev.is_all_day,
            }
            for ev in events
        ]
    finally:
        session.close()


@router.get("/approval-summary")
def get_approval_summary(
    current_user: dict = Depends(require_permission(Permission.APPROVALS)),
):
    """取得待審核項目數量"""
    session = get_session()
    try:
        pending_leaves = session.query(LeaveRecord).filter(
            LeaveRecord.is_approved.is_(None),
        ).count()

        pending_overtimes = session.query(OvertimeRecord).filter(
            OvertimeRecord.is_approved.is_(None),
        ).count()

        return {
            "pending_leaves": pending_leaves,
            "pending_overtimes": pending_overtimes,
            "total": pending_leaves + pending_overtimes,
        }
    finally:
        session.close()
