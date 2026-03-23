"""Shared dashboard / notification query service."""

from calendar import monthrange
from datetime import date, timedelta

from cachetools import TTLCache
from sqlalchemy import and_, case, func

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
# 通知摘要被前端每 10 秒輪詢，用短暫快取避免高頻 DB 查詢
NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS = 15


class DashboardQueryService:
    def __init__(self):
        # 依 user_permissions 分組快取，最多 128 種不同權限組合
        self._notification_cache: TTLCache = TTLCache(
            maxsize=128, ttl=NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS
        )
        # 審核摘要與行事曆是全系統共用資料（非個人化），可跨不同權限組合共用快取
        # maxsize=1：同一天只需快取一份；key = date ISO string
        self._approval_cache: TTLCache = TTLCache(
            maxsize=1, ttl=NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS
        )
        # maxsize=8：依 (date, days) 組合，最多 8 種查詢視窗
        self._events_cache: TTLCache = TTLCache(
            maxsize=8, ttl=NOTIFICATION_SUMMARY_CACHE_TTL_SECONDS
        )

    def _priority_for_count(self, count: int) -> str:
        if count >= 5:
            return "high"
        if count > 0:
            return "medium"
        return "low"

    def build_upcoming_events(self, session, *, days: int = 7, today: date | None = None) -> list[dict]:
        today = today or date.today()
        cache_key = (today.isoformat(), days)
        cached = self._events_cache.get(cache_key)
        if cached is not None:
            return cached

        end_date = today + timedelta(days=days)
        events = session.query(SchoolEvent).filter(
            SchoolEvent.is_active == True,
            SchoolEvent.event_date >= today,
            SchoolEvent.event_date <= end_date,
        ).order_by(SchoolEvent.event_date).all()

        result = [
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
        self._events_cache[cache_key] = result
        return result

    def build_approval_summary(self, session, *, today: date | None = None) -> dict:
        today = today or date.today()
        cache_key = today.isoformat()
        cached = self._approval_cache.get(cache_key)
        if cached is not None:
            return cached

        first_day = date(today.year, today.month, 1)
        _, last = monthrange(today.year, today.month)
        last_day = date(today.year, today.month, last)

        # 單次條件聚合，同時取得全部待審 + 本月待審（4 次 → 2 次）
        leave_row = session.query(
            func.count().label("total"),
            func.count(case(
                (and_(LeaveRecord.start_date >= first_day, LeaveRecord.start_date <= last_day), LeaveRecord.id),
                else_=None,
            )).label("this_month"),
        ).filter(LeaveRecord.is_approved.is_(None)).first()
        pending_leaves = leave_row.total if leave_row else 0
        this_month_leaves = leave_row.this_month if leave_row else 0

        ot_row = session.query(
            func.count().label("total"),
            func.count(case(
                (and_(OvertimeRecord.overtime_date >= first_day, OvertimeRecord.overtime_date <= last_day), OvertimeRecord.id),
                else_=None,
            )).label("this_month"),
        ).filter(OvertimeRecord.is_approved.is_(None)).first()
        pending_overtimes = ot_row.total if ot_row else 0
        this_month_overtimes = ot_row.this_month if ot_row else 0

        pending_corrections = session.query(PunchCorrectionRequest).filter(
            PunchCorrectionRequest.is_approved.is_(None),
        ).count()

        result = {
            "pending_leaves": pending_leaves,
            "pending_overtimes": pending_overtimes,
            "pending_punch_corrections": pending_corrections,
            "total": pending_leaves + pending_overtimes + pending_corrections,
            "this_month_pending_leaves": this_month_leaves,
            "this_month_pending_overtimes": this_month_overtimes,
        }
        self._approval_cache[cache_key] = result
        return result

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

        if has_permission(user_permissions, Permission.STUDENTS_READ):
            sections["student_attendance_summary"] = self.build_student_attendance_summary(session)

        if has_permission(user_permissions, Permission.ACTIVITY_READ):
            sections["activity_stats"] = self.build_activity_stats(session)

        return sections

    def build_notification_summary(self, session, *, user_permissions: int) -> dict:
        cached = self._notification_cache.get(user_permissions)
        if cached is not None:
            return cached
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

        result = {
            "total_badge": sum(item["count"] for item in action_items),
            "action_items": action_items,
            "reminders": reminders,
        }
        self._notification_cache[user_permissions] = result
        return result


dashboard_query_service = DashboardQueryService()
