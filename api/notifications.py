"""Admin notification center aggregation router."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from api.approvals import (
    build_approval_summary_data,
    build_probation_alerts_data,
    build_upcoming_events_data,
)
from models.database import get_session
from services.activity_service import ActivityService
from utils.auth import get_current_user
from utils.permissions import Permission, has_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

_activity_service = ActivityService()


def _priority_for_count(count: int) -> str:
    if count >= 5:
        return "high"
    if count > 0:
        return "medium"
    return "low"


@router.get("/summary")
def get_notification_summary(
    current_user: dict = Depends(get_current_user),
):
    """聚合後台待辦與提醒通知。"""
    if current_user.get("role") == "teacher":
        raise HTTPException(status_code=403, detail="教師帳號不可直接存取管理端 API")

    session = get_session()
    try:
        user_permissions = current_user.get("permissions", 0)
        action_items = []
        reminders = []

        if has_permission(user_permissions, Permission.APPROVALS):
            approval_summary = build_approval_summary_data(session)
            if approval_summary["total"] > 0:
                action_items.append({
                    "type": "approval",
                    "title": "待審核項目",
                    "count": approval_summary["total"],
                    "route": "/approvals",
                    "priority": _priority_for_count(approval_summary["total"]),
                    "breakdown": {
                        "leaves": approval_summary["pending_leaves"],
                        "overtimes": approval_summary["pending_overtimes"],
                        "punch_corrections": approval_summary["pending_punch_corrections"],
                    },
                })

        if has_permission(user_permissions, Permission.ACTIVITY_READ):
            unread_inquiries = _activity_service.get_unread_inquiries_count(session)
            if unread_inquiries > 0:
                action_items.append({
                    "type": "activity_inquiry",
                    "title": "家長未讀提問",
                    "count": unread_inquiries,
                    "route": "/activity/inquiries",
                    "priority": _priority_for_count(unread_inquiries),
                })

        if has_permission(user_permissions, Permission.CALENDAR):
            events = build_upcoming_events_data(session, days=7)
            if events:
                reminders.append({
                    "type": "calendar",
                    "title": "近期行事曆",
                    "route": "/calendar",
                    "priority": "low",
                    "items": [
                        {
                            "id": item["id"],
                            "label": item["title"],
                            "date": item["event_date"],
                            "meta": item["event_type_label"],
                        }
                        for item in events
                    ],
                })

        if has_permission(user_permissions, Permission.EMPLOYEES_READ):
            probation = build_probation_alerts_data(session)
            if probation["employees"]:
                reminders.append({
                    "type": "probation",
                    "title": "下月試用期到期",
                    "route": "/employees",
                    "priority": "medium",
                    "items": [
                        {
                            "id": item["id"],
                            "label": f"{item['employee_id']} {item['name']}",
                            "date": item["probation_end_date"],
                            "meta": f"剩餘 {item['days_remaining']} 天",
                        }
                        for item in probation["employees"]
                    ],
                })

        return {
            "total_badge": sum(item["count"] for item in action_items),
            "action_items": action_items,
            "reminders": reminders,
        }
    finally:
        session.close()
