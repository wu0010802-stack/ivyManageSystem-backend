"""
Approval summary / dashboard auxiliary router.
"""

import logging

from fastapi import APIRouter, Depends, Query

from models.database import get_session
from services.dashboard_query_service import (
    EVENT_TYPE_LABELS,
    dashboard_query_service,
)
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["approvals"])

_EVENT_TYPE_LABELS = EVENT_TYPE_LABELS


@router.get("/upcoming-events")
def get_upcoming_events(
    days: int = Query(7, ge=1, le=30),
    current_user: dict = Depends(require_staff_permission(Permission.DASHBOARD)),
):
    """取得近期行事曆事件（供儀表板使用）"""
    session = get_session()
    try:
        return dashboard_query_service.build_upcoming_events(session, days=days)
    finally:
        session.close()


@router.get("/approval-summary")
def get_approval_summary(
    current_user: dict = Depends(require_staff_permission(Permission.APPROVALS)),
):
    """取得待審核項目數量"""
    session = get_session()
    try:
        return dashboard_query_service.build_approval_summary(session)
    finally:
        session.close()


@router.get("/student-attendance-summary")
def get_student_attendance_summary(
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    """取得今日全園學生出勤摘要（供儀表板使用）"""
    session = get_session()
    try:
        return dashboard_query_service.build_student_attendance_summary(session)
    finally:
        session.close()
