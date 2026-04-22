"""
services/activity_service.py — 課後才藝報名業務邏輯
"""

import logging
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, case

TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def _now_taipei_naive() -> datetime:
    """候補狀態機與 confirm_deadline 用的「當下」。

    Why: 原本多處 datetime.now() 在 UTC 部署下會與家長端顯示的台灣時間差 8h，
    造成 LINE 通知 deadline 錯亂、逾期判定也差一個 timezone。RegistrationCourse
    相關欄位都是 naive DateTime，統一用台灣時間的 naive 表示。
    """
    return datetime.now(TAIPEI_TZ).replace(tzinfo=None)


# 候補升正式的「佔位」狀態集合：enrolled + promoted_pending 皆佔容量，
# 決定「還有無名額」時務必 IN 兩者；統計/出席/收入等語意只算 enrolled。
OCCUPYING_STATUSES = ("enrolled", "promoted_pending")


def _get_confirm_window_hours() -> int:
    """確認窗口長度（小時）。預設 48h，透過 env 覆寫。"""
    try:
        val = int(os.getenv("ACTIVITY_WAITLIST_CONFIRM_WINDOW_HOURS", "48"))
        return val if val > 0 else 48
    except (TypeError, ValueError):
        return 48


def _get_reminder_offset_hours() -> int:
    """發送「剩餘 X 小時」提醒的 deadline 前置時數。預設 24h。"""
    try:
        val = int(os.getenv("ACTIVITY_WAITLIST_REMINDER_OFFSET_HOURS", "24"))
        return val if val > 0 else 24
    except (TypeError, ValueError):
        return 24


from models.activity import (
    ActivityCourse,
    ActivitySupply,
    ActivityRegistration,
    RegistrationCourse,
    RegistrationSupply,
    ParentInquiry,
    RegistrationChange,
    ActivityRegistrationSettings,
    ActivityPaymentRecord,
    ActivityPosDailyClose,
)
from services.report_cache_service import report_cache_service

logger = logging.getLogger(__name__)

ACTIVITY_SUMMARY_CACHE_CATEGORIES = ("activity_stats_summary",)
ACTIVITY_DASHBOARD_CACHE_CATEGORIES = (
    "activity_stats_summary",
    "activity_stats_charts",
    "activity_dashboard_table",
)
ACTIVITY_STATS_SUMMARY_CACHE_TTL_SECONDS = 300
ACTIVITY_STATS_CHARTS_CACHE_TTL_SECONDS = 600
ACTIVITY_DASHBOARD_TABLE_CACHE_TTL_SECONDS = 1800


class ActivityService:
    def __init__(self):
        self._line_svc = None

    def set_line_service(self, svc) -> None:
        self._line_svc = svc

    def get_unread_inquiries_count(self, session) -> int:
        """取得未讀家長提問數量。"""
        return (
            session.query(func.count(ParentInquiry.id))
            .filter(ParentInquiry.is_read.is_(False))
            .scalar()
            or 0
        )

    def _active_course_query(self, session, course_id: int):
        """回傳指定課程的有效報名關聯查詢。"""
        return (
            session.query(RegistrationCourse)
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                RegistrationCourse.course_id == course_id,
                ActivityRegistration.is_active.is_(True),
            )
        )

    def count_active_course_registrations(
        self,
        session,
        course_id: int,
        *,
        status: str | None = None,
        statuses: tuple | list | None = None,
    ) -> int:
        """計算指定課程的有效報名數。

        status（單值）與 statuses（多值）擇一使用；兩者都給時以 statuses 為準。
        決定容量佔用時請傳 statuses=OCCUPYING_STATUSES。
        """
        query = self._active_course_query(session, course_id)
        if statuses is not None:
            query = query.filter(RegistrationCourse.status.in_(list(statuses)))
        elif status is not None:
            query = query.filter(RegistrationCourse.status == status)
        return query.count()

    def count_occupying_registrations(self, session, course_id: int) -> int:
        """計算佔用容量的報名數（enrolled + promoted_pending）。"""
        return self.count_active_course_registrations(
            session, course_id, statuses=OCCUPYING_STATUSES
        )

    def invalidate_summary_cache(self, session) -> int:
        return report_cache_service.invalidate_categories(
            session,
            *ACTIVITY_SUMMARY_CACHE_CATEGORIES,
        )

    def invalidate_dashboard_caches(self, session) -> int:
        return report_cache_service.invalidate_categories(
            session,
            *ACTIVITY_DASHBOARD_CACHE_CATEGORIES,
        )

    # ------------------------------------------------------------------ #
    # 統計儀表板
    # ------------------------------------------------------------------ #

    def _compute_stats_summary(self, session) -> dict:
        """取得儀表板摘要統計。"""
        today = datetime.now(TAIPEI_TZ).date()
        active_registration_filter = ActivityRegistration.is_active.is_(True)

        summary_row = session.execute(
            select(
                select(func.count(ActivityRegistration.id))
                .where(active_registration_filter)
                .scalar_subquery()
                .label("total_registrations"),
                select(func.count(RegistrationCourse.id))
                .join(
                    ActivityRegistration,
                    RegistrationCourse.registration_id == ActivityRegistration.id,
                )
                .where(
                    RegistrationCourse.status == "enrolled",
                    active_registration_filter,
                )
                .scalar_subquery()
                .label("total_enrollments"),
                select(func.count(RegistrationCourse.id))
                .join(
                    ActivityRegistration,
                    RegistrationCourse.registration_id == ActivityRegistration.id,
                )
                .where(
                    RegistrationCourse.status == "waitlist",
                    active_registration_filter,
                )
                .scalar_subquery()
                .label("total_waitlist"),
                select(func.count(RegistrationSupply.id))
                .join(
                    ActivityRegistration,
                    RegistrationSupply.registration_id == ActivityRegistration.id,
                )
                .where(active_registration_filter)
                .scalar_subquery()
                .label("total_supply_orders"),
                select(func.count(ActivityRegistration.id))
                .where(
                    active_registration_filter,
                    func.date(ActivityRegistration.created_at) == today,
                )
                .scalar_subquery()
                .label("today_new"),
                select(func.coalesce(func.sum(RegistrationCourse.price_snapshot), 0))
                .join(
                    ActivityRegistration,
                    RegistrationCourse.registration_id == ActivityRegistration.id,
                )
                .where(
                    ActivityRegistration.is_paid.is_(True),
                    active_registration_filter,
                    RegistrationCourse.status == "enrolled",
                )
                .scalar_subquery()
                .label("paid_revenue_courses"),
                select(func.coalesce(func.sum(RegistrationSupply.price_snapshot), 0))
                .join(
                    ActivityRegistration,
                    RegistrationSupply.registration_id == ActivityRegistration.id,
                )
                .where(
                    ActivityRegistration.is_paid.is_(True),
                    active_registration_filter,
                )
                .scalar_subquery()
                .label("paid_revenue_supplies"),
                select(func.coalesce(func.sum(RegistrationCourse.price_snapshot), 0))
                .join(
                    ActivityRegistration,
                    RegistrationCourse.registration_id == ActivityRegistration.id,
                )
                .where(
                    ActivityRegistration.is_paid.is_(False),
                    active_registration_filter,
                    RegistrationCourse.status == "enrolled",
                )
                .scalar_subquery()
                .label("unpaid_revenue_courses"),
                select(func.coalesce(func.sum(RegistrationSupply.price_snapshot), 0))
                .join(
                    ActivityRegistration,
                    RegistrationSupply.registration_id == ActivityRegistration.id,
                )
                .where(
                    ActivityRegistration.is_paid.is_(False),
                    active_registration_filter,
                )
                .scalar_subquery()
                .label("unpaid_revenue_supplies"),
                select(func.coalesce(func.sum(ActivityCourse.capacity), 0))
                .where(ActivityCourse.is_active.is_(True))
                .scalar_subquery()
                .label("total_capacity"),
                select(func.count(ParentInquiry.id))
                .where(ParentInquiry.is_read.is_(False))
                .scalar_subquery()
                .label("unread_inquiries"),
            )
        ).one()

        total_enrollments = int(summary_row.total_enrollments or 0)
        total_capacity = int(summary_row.total_capacity or 0)
        enrollment_rate = (
            round(total_enrollments / total_capacity * 100, 1)
            if total_capacity > 0
            else 0.0
        )

        return {
            "totalRegistrations": int(summary_row.total_registrations or 0),
            "totalEnrollments": total_enrollments,
            "totalWaitlist": int(summary_row.total_waitlist or 0),
            "totalSupplyOrders": int(summary_row.total_supply_orders or 0),
            "todayNewRegistrations": int(summary_row.today_new or 0),
            "totalRevenue": int(summary_row.paid_revenue_courses or 0)
            + int(summary_row.paid_revenue_supplies or 0),
            "totalUnpaid": int(summary_row.unpaid_revenue_courses or 0)
            + int(summary_row.unpaid_revenue_supplies or 0),
            "enrollmentRate": enrollment_rate,
            "unreadInquiries": int(summary_row.unread_inquiries or 0),
        }

    def _compute_stats_charts(self, session) -> dict:
        """取得儀表板圖表資料。"""
        chart_window_start = datetime.now(TAIPEI_TZ).date() - timedelta(days=29)

        # 每日報名趨勢（最近 30 個有資料日期）
        daily_rows = (
            session.query(
                func.date(ActivityRegistration.created_at).label("d"),
                func.count(ActivityRegistration.id).label("c"),
            )
            .filter(
                ActivityRegistration.is_active.is_(True),
                func.date(ActivityRegistration.created_at) >= chart_window_start,
            )
            .group_by(func.date(ActivityRegistration.created_at))
            .order_by(func.date(ActivityRegistration.created_at).desc())
            .limit(30)
            .all()
        )
        daily_stats = [
            {"date": str(row.d), "count": row.c} for row in reversed(daily_rows)
        ]

        # 熱門課程（top 5）
        top_courses_rows = (
            session.query(
                ActivityCourse.name,
                func.count(RegistrationCourse.id).label("c"),
            )
            .join(RegistrationCourse, ActivityCourse.id == RegistrationCourse.course_id)
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                RegistrationCourse.status == "enrolled",
                ActivityRegistration.is_active.is_(True),
            )
            .group_by(ActivityCourse.name)
            .order_by(func.count(RegistrationCourse.id).desc())
            .limit(5)
            .all()
        )
        top_courses = [{"name": row[0], "count": row[1]} for row in top_courses_rows]

        return {
            "daily": daily_stats,
            "topCourses": top_courses,
        }

    def get_stats_summary(self, session, *, force_refresh: bool = False) -> dict:
        return report_cache_service.get_or_build(
            session,
            category="activity_stats_summary",
            ttl_seconds=ACTIVITY_STATS_SUMMARY_CACHE_TTL_SECONDS,
            force_refresh=force_refresh,
            builder=lambda: self._compute_stats_summary(session),
        )

    def get_stats_charts(self, session, *, force_refresh: bool = False) -> dict:
        return report_cache_service.get_or_build(
            session,
            category="activity_stats_charts",
            ttl_seconds=ACTIVITY_STATS_CHARTS_CACHE_TTL_SECONDS,
            force_refresh=force_refresh,
            builder=lambda: self._compute_stats_charts(session),
        )

    def get_stats(self, session, *, force_refresh: bool = False) -> dict:
        return {
            "statistics": self.get_stats_summary(session, force_refresh=force_refresh),
            "charts": self.get_stats_charts(session, force_refresh=force_refresh),
            "attendance_stats": self.get_attendance_stats(session),
        }

    def get_attendance_stats(self, session) -> dict:
        """取得課程出席率統計（SQL 直接 GROUP BY 課程，省去 Python 端二次聚合）。"""
        from models.activity import ActivitySession, ActivityAttendance

        rows = (
            session.query(
                ActivityCourse.name.label("course_name"),
                func.count(ActivitySession.id.distinct()).label("sessions"),
                func.count(ActivityAttendance.id).label("total"),
                func.sum(
                    case((ActivityAttendance.is_present.is_(True), 1), else_=0)
                ).label("present"),
            )
            .filter(ActivityCourse.is_active.is_(True))
            .join(ActivitySession, ActivityCourse.id == ActivitySession.course_id)
            .join(
                ActivityAttendance, ActivitySession.id == ActivityAttendance.session_id
            )
            .group_by(ActivityCourse.name)
            .all()
        )

        if not rows:
            return {"total_sessions": 0, "avg_attendance_rate": 0.0, "by_course": []}

        total_sessions = sum(row.sessions or 0 for row in rows)
        total_present = sum(row.present or 0 for row in rows)
        total_records = sum(row.total or 0 for row in rows)
        avg_rate = round(total_present / total_records, 2) if total_records > 0 else 0.0

        by_course = [
            {
                "course_name": row.course_name,
                "sessions": row.sessions or 0,
                "avg_rate": (
                    round((row.present or 0) / row.total, 2) if row.total else 0.0
                ),
            }
            for row in rows
        ]
        by_course.sort(key=lambda x: x["avg_rate"], reverse=True)

        return {
            "total_sessions": total_sessions,
            "avg_attendance_rate": avg_rate,
            "by_course": by_course,
        }

    # ------------------------------------------------------------------ #
    # 統計儀表板表格 (依班級)
    # ------------------------------------------------------------------ #

    _GRADE_TARGET_MAPPING = {
        "大班": 100,
        "中班": 90,
        "小班": 80,
        "幼娃班": 70,
        "娃娃班": 70,
        "幼幼班": 70,
        "幼班": 70,
    }

    def _get_grade_target_percent(self, grade_name: str) -> int:
        """依年級名稱查對應達成率目標。"""
        for key, pct in self._GRADE_TARGET_MAPPING.items():
            if key in grade_name:
                return pct
        return 0

    def _query_classroom_stats(self, session, courses, school_year: int, semester: int):
        """一次查出所有班級的在籍人數、各課程報名數及班導師。"""
        from models.classroom import Classroom, Student
        from models.employee import Employee

        # 查詢一：一次 JOIN 同時建立 student_count_map 和 classrooms_by_grade
        student_count_map = {}
        classrooms_by_grade: dict = defaultdict(list)
        for cls, teacher_name, student_count in (
            session.query(
                Classroom,
                Employee.name.label("teacher_name"),
                func.count(Student.id).label("student_count"),
            )
            .outerjoin(Employee, Classroom.head_teacher_id == Employee.id)
            .outerjoin(
                Student,
                (Student.classroom_id == Classroom.id) & Student.is_active.is_(True),
            )
            .filter(
                Classroom.is_active.is_(True),
                Classroom.school_year == school_year,
                Classroom.semester == semester,
            )
            .group_by(Classroom.id, Employee.name)
            .all()
        ):
            student_count_map[cls.id] = student_count
            classrooms_by_grade[cls.grade_id].append((cls, teacher_name))

        # 查詢二：enrollment_map 以 classroom_id FK 為 key（轉班後仍正確，字串 class_name
        # 可能過時）；限定同學期且排除 rejected。
        enrollment_map: dict = {}
        for row in (
            session.query(
                ActivityRegistration.classroom_id,
                RegistrationCourse.course_id,
                func.count(RegistrationCourse.id).label("count"),
            )
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                ActivityRegistration.is_active.is_(True),
                ActivityRegistration.match_status != "rejected",
                ActivityRegistration.school_year == school_year,
                ActivityRegistration.semester == semester,
                ActivityRegistration.classroom_id.isnot(None),
                RegistrationCourse.status == "enrolled",
            )
            .group_by(ActivityRegistration.classroom_id, RegistrationCourse.course_id)
            .all()
        ):
            enrollment_map[(row.classroom_id, row.course_id)] = row.count

        return student_count_map, enrollment_map, classrooms_by_grade

    def _build_grade_rows(
        self, grades, courses, student_count_map, enrollment_map, classrooms_by_grade
    ):
        """組裝各年級列表，回傳 (result_grades, gt_student_count, gt_courses, gt_total_enrollments)。"""
        result_grades = []
        gt_student_count = 0
        gt_courses = {str(c.id): 0 for c in courses}
        gt_total_enrollments = 0

        for grade in grades:
            grade_name = grade.name
            target_pct = self._get_grade_target_percent(grade_name)
            classrooms_data = classrooms_by_grade.get(grade.id, [])
            if not classrooms_data:
                continue

            sub_student_count = 0
            sub_courses = {str(c.id): 0 for c in courses}
            sub_total_enrollments = 0
            classroom_list = []

            for cls, teacher_name in classrooms_data:
                cls_student_count = student_count_map.get(cls.id, 0)
                cls_enrollments = 0
                cls_course_data = {}
                for c in courses:
                    c_id_str = str(c.id)
                    count = enrollment_map.get((cls.id, c.id), 0)
                    cls_course_data[c_id_str] = count
                    cls_enrollments += count
                    sub_courses[c_id_str] += count
                    gt_courses[c_id_str] += count

                cls_ratio = (
                    int(round(cls_enrollments / cls_student_count * 100))
                    if cls_student_count > 0
                    else 0
                )
                sub_student_count += cls_student_count
                sub_total_enrollments += cls_enrollments
                gt_student_count += cls_student_count
                gt_total_enrollments += cls_enrollments

                classroom_list.append(
                    {
                        "classroom_id": cls.id,
                        "classroom_name": cls.name,
                        "teacher_name": teacher_name or "",
                        "student_count": cls_student_count,
                        "courses": cls_course_data,
                        "total_enrollments": cls_enrollments,
                        "ratio": cls_ratio,
                    }
                )

            sub_ratio = (
                int(round(sub_total_enrollments / sub_student_count * 100))
                if sub_student_count > 0
                else 0
            )
            sub_bonus = 1000 if target_pct > 0 and sub_ratio >= target_pct else 0
            sub_points = target_pct if sub_bonus else 0

            result_grades.append(
                {
                    "grade_id": grade.id,
                    "grade_name": grade_name,
                    "target_percent": target_pct,
                    "classrooms": classroom_list,
                    "subtotal": {
                        "student_count": sub_student_count,
                        "courses": sub_courses,
                        "total_enrollments": sub_total_enrollments,
                        "ratio": sub_ratio,
                        "bonus": sub_bonus,
                        "points": sub_points,
                    },
                }
            )

        return result_grades, gt_student_count, gt_courses, gt_total_enrollments

    def _compute_dashboard_table(
        self, session, school_year: int, semester: int
    ) -> dict:
        """取得課後才藝儀表板統計表格（含依班級與年級的小計與達成率）"""
        from models.classroom import ClassGrade

        courses = (
            session.query(ActivityCourse)
            .filter(ActivityCourse.is_active.is_(True))
            .order_by(ActivityCourse.id)
            .all()
        )
        course_list = [{"id": c.id, "name": c.name} for c in courses]

        grades = (
            session.query(ClassGrade)
            .filter(ClassGrade.is_active.is_(True))
            .order_by(ClassGrade.sort_order)
            .all()
        )

        student_count_map, enrollment_map, classrooms_by_grade = (
            self._query_classroom_stats(session, courses, school_year, semester)
        )
        result_grades, gt_student_count, gt_courses, gt_total_enrollments = (
            self._build_grade_rows(
                grades, courses, student_count_map, enrollment_map, classrooms_by_grade
            )
        )

        gt_ratio = (
            int(round(gt_total_enrollments / gt_student_count * 100))
            if gt_student_count > 0
            else 0
        )
        grand_total = {
            "student_count": gt_student_count,
            "courses": gt_courses,
            "total_enrollments": gt_total_enrollments,
            "ratio": gt_ratio,
        }

        return {
            "courses": course_list,
            "grades": result_grades,
            "grand_total": grand_total,
            "school_year": school_year,
            "semester": semester,
        }

    def get_dashboard_table(
        self,
        session,
        *,
        school_year: int,
        semester: int,
        force_refresh: bool = False,
    ) -> dict:
        return report_cache_service.get_or_build(
            session,
            category="activity_dashboard_table",
            ttl_seconds=ACTIVITY_DASHBOARD_TABLE_CACHE_TTL_SECONDS,
            params={"school_year": school_year, "semester": semester},
            force_refresh=force_refresh,
            builder=lambda: self._compute_dashboard_table(
                session, school_year, semester
            ),
        )

    # ------------------------------------------------------------------ #
    # 候補升正式
    # ------------------------------------------------------------------ #

    def promote_waitlist(
        self, session, registration_id: int, course_id: int
    ) -> tuple[str, str]:
        """
        管理員手動升正式：從 waitlist 或 promoted_pending 直接升為 enrolled。
        使用 with_for_update() 防止並發超額。
        回傳 (student_name, course_name)，失敗時拋 ValueError。
        """
        row = (
            session.query(RegistrationCourse, ActivityRegistration.student_name)
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                RegistrationCourse.registration_id == registration_id,
                RegistrationCourse.course_id == course_id,
                RegistrationCourse.status.in_(["waitlist", "promoted_pending"]),
                ActivityRegistration.is_active.is_(True),
            )
            .with_for_update()
            .first()
        )
        if not row:
            raise ValueError("報名課程項目不存在或非候補/待確認狀態")
        rc, student_name = row

        course = (
            session.query(ActivityCourse)
            .filter(ActivityCourse.id == course_id)
            .with_for_update()
            .first()
        )
        if not course:
            raise ValueError("課程不存在")

        # 升 enrolled 的容量閘：需看「非此列」的佔位數（否則 promoted_pending→enrolled 會自我阻擋）
        occupying_others = (
            self._active_course_query(session, course_id)
            .filter(RegistrationCourse.status.in_(list(OCCUPYING_STATUSES)))
            .filter(RegistrationCourse.id != rc.id)
            .count()
        )
        if course.capacity is not None and occupying_others >= course.capacity:
            raise ValueError("課程容量已滿，無法升為正式")

        rc.status = "enrolled"
        # 管理員直升視同已確認，清掉待確認計時欄位
        rc.confirm_deadline = None
        rc.reminder_sent_at = None
        return (student_name or str(registration_id), course.name)

    # ------------------------------------------------------------------ #
    # 候補轉正 24h 確認窗狀態機
    # ------------------------------------------------------------------ #

    def confirm_waitlist_promotion(
        self, session, registration_id: int, course_id: int
    ) -> tuple[str, str]:
        """家長確認升正式。狀態必須為 promoted_pending 且未逾期。"""
        now = _now_taipei_naive()
        row = (
            session.query(RegistrationCourse, ActivityRegistration.student_name)
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                RegistrationCourse.registration_id == registration_id,
                RegistrationCourse.course_id == course_id,
                ActivityRegistration.is_active.is_(True),
            )
            .with_for_update()
            .first()
        )
        if not row:
            raise ValueError("NOT_FOUND")
        rc, student_name = row

        if rc.status == "enrolled":
            raise ValueError("ALREADY_CONFIRMED")
        if rc.status != "promoted_pending":
            raise ValueError("NOT_PENDING")
        if rc.confirm_deadline and rc.confirm_deadline < now:
            raise ValueError("EXPIRED")

        course = (
            session.query(ActivityCourse)
            .filter(ActivityCourse.id == course_id)
            .with_for_update()
            .first()
        )
        if not course:
            raise ValueError("NOT_FOUND")

        rc.status = "enrolled"
        rc.confirm_deadline = None
        rc.reminder_sent_at = None
        return (student_name or str(registration_id), course.name)

    def decline_waitlist_promotion(
        self, session, registration_id: int, course_id: int, operator: str = "parent"
    ) -> tuple[str, str]:
        """家長放棄升正式。刪除該 RegistrationCourse 並自動遞補下一位。"""
        row = (
            session.query(RegistrationCourse, ActivityRegistration.student_name)
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                RegistrationCourse.registration_id == registration_id,
                RegistrationCourse.course_id == course_id,
                ActivityRegistration.is_active.is_(True),
            )
            .with_for_update()
            .first()
        )
        if not row:
            raise ValueError("NOT_FOUND")
        rc, student_name = row

        if rc.status == "enrolled":
            raise ValueError("ALREADY_CONFIRMED")
        if rc.status != "promoted_pending":
            raise ValueError("NOT_PENDING")

        course = (
            session.query(ActivityCourse).filter(ActivityCourse.id == course_id).first()
        )
        course_name = course.name if course else f"course_{course_id}"

        session.delete(rc)
        self.log_change(
            session,
            registration_id,
            student_name or str(registration_id),
            "放棄候補轉正",
            f"課程「{course_name}」：{operator} 放棄升正式",
            operator,
        )
        session.flush()
        # 釋出名額，自動遞補下一位候補
        self._auto_promote_first_waitlist(session, course_id)
        return (student_name or str(registration_id), course_name)

    def sweep_expired_pending_promotions(self, session) -> dict:
        """掃描過期未確認的 promoted_pending，逾期者刪除並遞補下一位；
        同時發送 T-X 小時剩餘提醒（只發一次，以 reminder_sent_at 標註）。

        回傳 {"expired": N, "reminded": M}，由背景排程呼叫。
        """
        now = _now_taipei_naive()
        reminder_offset = timedelta(hours=_get_reminder_offset_hours())
        reminder_threshold = now + reminder_offset

        # 1. 過期者：刪除 + 遞補
        expired_rows = (
            session.query(RegistrationCourse, ActivityRegistration.student_name)
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                RegistrationCourse.status == "promoted_pending",
                RegistrationCourse.confirm_deadline.isnot(None),
                RegistrationCourse.confirm_deadline < now,
                ActivityRegistration.is_active.is_(True),
            )
            .with_for_update(skip_locked=True)
            .all()
        )
        expired_count = 0
        # 計數而非 set：同課多筆同時過期需呼叫 N 次遞補，否則會少補位
        expired_per_course: dict[int, int] = {}
        for rc, student_name in expired_rows:
            course = (
                session.query(ActivityCourse)
                .filter(ActivityCourse.id == rc.course_id)
                .first()
            )
            course_name = course.name if course else f"course_{rc.course_id}"
            reg_id = rc.registration_id
            course_id = rc.course_id
            session.delete(rc)
            self.log_change(
                session,
                reg_id,
                student_name or str(reg_id),
                "候補轉正逾期放棄",
                f"課程「{course_name}」逾期未確認，系統自動放棄",
                "system",
            )
            session.flush()
            expired_per_course[course_id] = expired_per_course.get(course_id, 0) + 1
            if self._line_svc is not None:
                try:
                    self._line_svc.notify_activity_waitlist_promotion_expired(
                        student_name or str(reg_id), course_name
                    )
                except Exception:
                    logger.exception("發送候補轉正逾期通知失敗 reg=%s", reg_id)
            expired_count += 1

        # 釋出 N 個位子 → 嘗試遞補 N 次（超過候補數時內層容量閘讓多餘呼叫變 no-op）
        for course_id, count in expired_per_course.items():
            for _ in range(count):
                self._auto_promote_first_waitlist(session, course_id)

        # 2. 即將到期者：發剩餘時間提醒（只發一次）
        reminder_rows = (
            session.query(RegistrationCourse, ActivityRegistration.student_name)
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                RegistrationCourse.status == "promoted_pending",
                RegistrationCourse.confirm_deadline.isnot(None),
                RegistrationCourse.confirm_deadline >= now,
                RegistrationCourse.confirm_deadline <= reminder_threshold,
                RegistrationCourse.reminder_sent_at.is_(None),
                ActivityRegistration.is_active.is_(True),
            )
            .all()
        )
        reminded_count = 0
        for rc, student_name in reminder_rows:
            course = (
                session.query(ActivityCourse)
                .filter(ActivityCourse.id == rc.course_id)
                .first()
            )
            course_name = course.name if course else f"course_{rc.course_id}"
            if self._line_svc is not None:
                try:
                    self._line_svc.notify_activity_waitlist_promotion_reminder(
                        student_name or str(rc.registration_id),
                        course_name,
                        rc.confirm_deadline,
                    )
                except Exception:
                    logger.exception(
                        "發送候補轉正提醒失敗 reg=%s course=%s",
                        rc.registration_id,
                        rc.course_id,
                    )
            rc.reminder_sent_at = now
            reminded_count += 1

        return {"expired": expired_count, "reminded": reminded_count}

    # ------------------------------------------------------------------ #
    # 軟刪除報名（含自動候補升位）
    # ------------------------------------------------------------------ #

    def delete_registration(
        self,
        session,
        registration_id: int,
        operator: str,
        force_refund: bool = False,
    ):
        """軟刪除報名，並對每門正式課程嘗試自動升位候補。

        若 paid_amount > 0：
          - force_refund=False → 拋 ValueError，迫使呼叫端先退費或確認
          - force_refund=True  → 自動寫一筆「系統補齊」退費紀錄沖帳

        Why: 原本軟刪時 paid_amount 仍掛著，帳務上成為幽靈金額；新機制保留
        完整 payment history 供稽核，同時強制操作者在刪除前面對退費責任。
        """
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == registration_id,
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        if not reg:
            raise ValueError("找不到報名資料")

        current_paid = reg.paid_amount or 0
        if current_paid > 0 and not force_refund:
            raise ValueError(
                f"此報名尚有已繳金額 NT${current_paid}，請先處理退費"
                f"或於刪除時指定 force_refund=true 自動沖帳"
            )

        # 取出佔位課程（enrolled + promoted_pending 皆佔容量，刪除後都需要遞補）
        occupying_courses = (
            session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.registration_id == registration_id,
                RegistrationCourse.status.in_(list(OCCUPYING_STATUSES)),
            )
            .all()
        )
        enrolled_course_ids = [rc.course_id for rc in occupying_courses]

        # 若有已繳金額，寫退費沖帳紀錄（不 DELETE 舊 payment 歷史）
        if current_paid > 0:
            # 以台灣時區取今日，避免 UTC 伺服器在近午夜台灣時間寫到錯的一天
            today = datetime.now(TAIPEI_TZ).date()
            # 已簽核日守衛：避免 snapshot 與 DB 失準。service 層改拋 ValueError
            # 由 router 轉為 HTTPException（避免 service 依賴 fastapi）。
            is_closed = (
                session.query(ActivityPosDailyClose.close_date)
                .filter(ActivityPosDailyClose.close_date == today)
                .first()
                is not None
            )
            if is_closed:
                raise ValueError(
                    f"今日（{today.isoformat()}）已完成日結簽核，無法自動沖帳。"
                    f"請先解鎖日結後再刪除此報名"
                )
            session.add(
                ActivityPaymentRecord(
                    registration_id=registration_id,
                    type="refund",
                    amount=current_paid,
                    payment_date=today,
                    payment_method="系統補齊",
                    notes="（刪除報名自動沖帳）",
                    operator=operator,
                )
            )
            reg.paid_amount = 0
            reg.is_paid = False

        # 軟刪除
        reg.is_active = False
        log_detail = "管理員刪除報名"
        if current_paid > 0:
            log_detail += f"（自動沖帳退費 NT${current_paid}）"
        self.log_change(
            session,
            registration_id,
            reg.student_name,
            "刪除報名",
            log_detail,
            operator,
        )
        session.flush()  # 先 flush，確保刪除生效後再升位

        # 對每門課嘗試升位候補第一位
        for course_id in enrolled_course_ids:
            self._auto_promote_first_waitlist(session, course_id)

    def _auto_promote_first_waitlist(self, session, course_id: int):
        """找出該課程候補排序最前的一筆，升為 promoted_pending 並設確認期限。

        - 若課程仍有名額（enrolled + promoted_pending < capacity）才升位
        - 升位後發 Line「已升正式待確認」通知，期限預設 48h
        - 無候補則靜默跳過
        """
        course = (
            session.query(ActivityCourse)
            .filter(ActivityCourse.id == course_id)
            .with_for_update()
            .first()
        )
        if not course:
            return
        occupying = (
            self._active_course_query(session, course_id)
            .filter(RegistrationCourse.status.in_(list(OCCUPYING_STATUSES)))
            .count()
        )
        if course.capacity is not None and occupying >= course.capacity:
            return  # 仍滿（有其他 promoted_pending 佔位），不升

        row = (
            session.query(RegistrationCourse, ActivityRegistration.student_name)
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                RegistrationCourse.course_id == course_id,
                RegistrationCourse.status == "waitlist",
                ActivityRegistration.is_active.is_(True),
            )
            .order_by(RegistrationCourse.id)
            .with_for_update()
            .first()
        )
        if not row:
            return
        rc, student_name = row
        now = _now_taipei_naive()
        deadline = now + timedelta(hours=_get_confirm_window_hours())
        rc.status = "promoted_pending"
        rc.promoted_at = now
        rc.confirm_deadline = deadline
        rc.reminder_sent_at = None

        self.log_change(
            session,
            rc.registration_id,
            student_name or str(rc.registration_id),
            "候補升正式（待確認）",
            f"課程「{course.name}」自動升為正式，家長須於 "
            f"{deadline.strftime('%Y-%m-%d %H:%M')} 前確認",
            "system",
        )
        logger.info(
            "候補自動升待確認：student=%s course=%s deadline=%s",
            student_name,
            course.name,
            deadline.isoformat(),
        )
        if self._line_svc is not None:
            try:
                self._line_svc.notify_activity_waitlist_promoted(
                    student_name or str(rc.registration_id),
                    course.name,
                    deadline,
                )
            except Exception:
                logger.exception(
                    "發送候補升正式通知失敗 reg=%s course=%s",
                    rc.registration_id,
                    course_id,
                )

    # ------------------------------------------------------------------ #
    # 記錄修改紀錄
    # ------------------------------------------------------------------ #

    def log_change(
        self,
        session,
        registration_id: int,
        student_name: str,
        change_type: str,
        description: str,
        changed_by: str,
    ):
        entry = RegistrationChange(
            registration_id=registration_id,
            student_name=student_name,
            change_type=change_type,
            description=description,
            changed_by=changed_by,
        )
        session.add(entry)

    # ------------------------------------------------------------------ #
    # 課程容量查詢
    # ------------------------------------------------------------------ #

    def check_course_capacity(self, session, course_id: int) -> tuple:
        """回傳 (capacity, occupying_count, has_vacancy)。

        occupying_count = enrolled + promoted_pending（兩者皆佔容量）。
        名稱保留 enrolled_count 以維持既有呼叫端 tuple 解包相容性，但語意已含待確認佔位。
        """
        course = (
            session.query(ActivityCourse).filter(ActivityCourse.id == course_id).first()
        )
        if not course:
            raise ValueError("課程不存在")

        occupying_count = self.count_occupying_registrations(session, course_id)
        capacity = course.capacity if course.capacity is not None else 999
        has_vacancy = occupying_count < capacity
        return capacity, occupying_count, has_vacancy


# Module-level singleton
activity_service = ActivityService()
