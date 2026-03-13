"""Shared dashboard / notification query service."""

from calendar import monthrange
from datetime import date, timedelta

from sqlalchemy import func

from models.database import (
    Employee,
    LeaveRecord,
    OvertimeRecord,
    PunchCorrectionRequest,
    SchoolEvent,
    Student,
    StudentAttendance,
)
from services.activity_service import activity_service
from services.report_cache_service import report_cache_service
from services.student_attendance_report import build_attendance_summary
from utils.permissions import Permission, has_permission


EVENT_TYPE_LABELS = {
    "meeting": "會議",
    "activity": "活動",
    "holiday": "假日",
    "general": "一般",
}
HOME_STUDENT_ATTENDANCE_CACHE_TTL_SECONDS = 300


class DashboardQueryService:
    def _priority_for_count(self, count: int) -> str:
        if count >= 5:
            return "high"
        if count > 0:
            return "medium"
        return "low"

    def build_upcoming_events(self, session, *, days: int = 7, today: date | None = None) -> list[dict]:
        today = today or date.today()
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
                "event_type_label": EVENT_TYPE_LABELS.get(ev.event_type, ev.event_type),
                "location": ev.location,
                "start_time": ev.start_time,
                "end_time": ev.end_time,
                "is_all_day": ev.is_all_day,
            }
            for ev in events
        ]

    def build_approval_summary(self, session, *, today: date | None = None) -> dict:
        pending_leaves = session.query(LeaveRecord).filter(
            LeaveRecord.is_approved.is_(None),
        ).count()

        pending_overtimes = session.query(OvertimeRecord).filter(
            OvertimeRecord.is_approved.is_(None),
        ).count()

        pending_corrections = session.query(PunchCorrectionRequest).filter(
            PunchCorrectionRequest.is_approved.is_(None),
        ).count()

        today = today or date.today()
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

    def build_probation_alerts(self, session, *, today: date | None = None) -> dict:
        today = today or date.today()
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

    def build_student_attendance_summary(self, session, *, today: date | None = None) -> dict:
        today = today or date.today()

        return report_cache_service.get_or_build(
            session,
            category="home_student_attendance_summary",
            ttl_seconds=HOME_STUDENT_ATTENDANCE_CACHE_TTL_SECONDS,
            params={"date": today.isoformat()},
            builder=lambda: self._compute_student_attendance_summary(session, today=today),
        )

    def _compute_student_attendance_summary(self, session, *, today: date) -> dict:
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
            **build_attendance_summary(total_students, status_counts),
        }

    def build_activity_stats(self, session) -> dict:
        return activity_service.get_stats(session)

    def build_home_sections(self, session, *, user_permissions: int, event_days: int = 7) -> dict:
        sections = {}

        if has_permission(user_permissions, Permission.APPROVALS):
            sections["approval_summary"] = self.build_approval_summary(session)

        if has_permission(user_permissions, Permission.CALENDAR):
            sections["upcoming_events"] = self.build_upcoming_events(session, days=event_days)

        if has_permission(user_permissions, Permission.EMPLOYEES_READ):
            sections["probation_alerts"] = self.build_probation_alerts(session)

        if has_permission(user_permissions, Permission.STUDENTS_READ):
            sections["student_attendance_summary"] = self.build_student_attendance_summary(session)

        if has_permission(user_permissions, Permission.ACTIVITY_READ):
            sections["activity_stats"] = self.build_activity_stats(session)

        return sections

    def build_notification_summary(self, session, *, user_permissions: int) -> dict:
        action_items = []
        reminders = []

        if has_permission(user_permissions, Permission.APPROVALS):
            approval_summary = self.build_approval_summary(session)
            if approval_summary["total"] > 0:
                action_items.append({
                    "type": "approval",
                    "title": "待審核項目",
                    "count": approval_summary["total"],
                    "route": "/approvals",
                    "priority": self._priority_for_count(approval_summary["total"]),
                    "breakdown": {
                        "leaves": approval_summary["pending_leaves"],
                        "overtimes": approval_summary["pending_overtimes"],
                        "punch_corrections": approval_summary["pending_punch_corrections"],
                        "this_month_pending_leaves": approval_summary["this_month_pending_leaves"],
                        "this_month_pending_overtimes": approval_summary["this_month_pending_overtimes"],
                    },
                })

        if has_permission(user_permissions, Permission.ACTIVITY_READ):
            unread_inquiries = activity_service.get_unread_inquiries_count(session)
            if unread_inquiries > 0:
                action_items.append({
                    "type": "activity_inquiry",
                    "title": "家長未讀提問",
                    "count": unread_inquiries,
                    "route": "/activity/inquiries",
                    "priority": self._priority_for_count(unread_inquiries),
                })

        if has_permission(user_permissions, Permission.CALENDAR):
            events = self.build_upcoming_events(session, days=7)
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
            probation = self.build_probation_alerts(session)
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


dashboard_query_service = DashboardQueryService()
