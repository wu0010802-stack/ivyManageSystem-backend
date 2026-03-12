"""
Approval summary router - pending counts for dashboard
"""

import logging
from calendar import monthrange
from datetime import date, timedelta
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func

from models.database import get_session, LeaveRecord, OvertimeRecord, SchoolEvent, PunchCorrectionRequest, Employee, Student, StudentAttendance
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

_STUDENT_ATTENDANCE_STATUSES = ("出席", "缺席", "病假", "事假", "遲到")


def _build_student_attendance_summary(total_students: int, raw_status_counts: dict[str, int]):
    """將今日學生點名分佈轉成儀表板摘要。"""
    status_counts = Counter({status: raw_status_counts.get(status, 0) for status in _STUDENT_ATTENDANCE_STATUSES})
    recorded_count = sum(status_counts.values())
    on_campus_count = status_counts["出席"] + status_counts["遲到"]
    leave_count = status_counts["病假"] + status_counts["事假"]
    unmarked_count = max(total_students - recorded_count, 0)

    return {
        "total_students": total_students,
        "recorded_count": recorded_count,
        "on_campus_count": on_campus_count,
        "present_count": status_counts["出席"],
        "late_count": status_counts["遲到"],
        "absent_count": status_counts["缺席"],
        "leave_count": leave_count,
        "sick_leave_count": status_counts["病假"],
        "personal_leave_count": status_counts["事假"],
        "unmarked_count": unmarked_count,
        "record_completion_rate": round((recorded_count / total_students) * 100, 1) if total_students else 0,
        "attendance_rate": round((on_campus_count / total_students) * 100, 1) if total_students else 0,
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

        pending_corrections = session.query(PunchCorrectionRequest).filter(
            PunchCorrectionRequest.is_approved.is_(None),
        ).count()

        today = date.today()
        first_day = date(today.year, today.month, 1)
        _, last = monthrange(today.year, today.month)
        last_day = date(today.year, today.month, last)

        this_month_leaves = session.query(LeaveRecord).filter(
            LeaveRecord.is_approved.is_(None),
            LeaveRecord.start_date >= first_day,
            LeaveRecord.start_date <= last_day,
        ).count()

        this_month_overtimes = session.query(OvertimeRecord).filter(
            OvertimeRecord.is_approved.is_(None),
            OvertimeRecord.overtime_date >= first_day,
            OvertimeRecord.overtime_date <= last_day,
        ).count()

        return {
            "pending_leaves": pending_leaves,
            "pending_overtimes": pending_overtimes,
            "pending_punch_corrections": pending_corrections,
            "total": pending_leaves + pending_overtimes + pending_corrections,
            "this_month_pending_leaves": this_month_leaves,
            "this_month_pending_overtimes": this_month_overtimes,
        }
    finally:
        session.close()


@router.get("/probation-alerts")
def get_probation_alerts(
    current_user: dict = Depends(require_permission(Permission.EMPLOYEES_READ)),
):
    """下個月即將到期的試用期員工"""
    session = get_session()
    try:
        today = date.today()
        # 計算下個月（含跨年）
        if today.month == 12:
            next_year, next_month = today.year + 1, 1
        else:
            next_year, next_month = today.year, today.month + 1

        _, last = monthrange(next_year, next_month)
        first_day = date(next_year, next_month, 1)
        last_day = date(next_year, next_month, last)

        employees = session.query(Employee).filter(
            Employee.is_active == True,
            Employee.probation_end_date >= first_day,
            Employee.probation_end_date <= last_day,
        ).order_by(Employee.probation_end_date).all()

        result = []
        for emp in employees:
            days_remaining = (emp.probation_end_date - today).days
            result.append({
                "id": emp.id,
                "employee_id": emp.employee_id,
                "name": emp.name,
                "hire_date": emp.hire_date.isoformat() if emp.hire_date else None,
                "probation_end_date": emp.probation_end_date.isoformat(),
                "days_remaining": days_remaining,
            })

        return {
            "next_month": f"{next_year}年{next_month}月",
            "employees": result,
        }
    finally:
        session.close()


@router.get("/student-attendance-summary")
def get_student_attendance_summary(
    current_user: dict = Depends(require_permission(Permission.STUDENTS_READ)),
):
    """取得今日全園學生出勤摘要（供儀表板使用）"""
    session = get_session()
    try:
        today = date.today()
        total_students = session.query(Student).filter(Student.is_active == True).count()
        rows = (
            session.query(StudentAttendance.status, func.count(StudentAttendance.id))
            .join(Student, StudentAttendance.student_id == Student.id)
            .filter(
                Student.is_active == True,
                StudentAttendance.date == today,
            )
            .group_by(StudentAttendance.status)
            .all()
        )
        status_counts = {status: count for status, count in rows}

        return {
            "date": today.isoformat(),
            **_build_student_attendance_summary(total_students, status_counts),
        }
    finally:
        session.close()
