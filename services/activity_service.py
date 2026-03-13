"""
services/activity_service.py — 課後才藝報名業務邏輯
"""

import logging
from datetime import datetime, date

from sqlalchemy import func

from models.activity import (
    ActivityCourse, ActivitySupply, ActivityRegistration,
    RegistrationCourse, RegistrationSupply,
    ParentInquiry, RegistrationChange, ActivityRegistrationSettings,
)

logger = logging.getLogger(__name__)


class ActivityService:
    def get_unread_inquiries_count(self, session) -> int:
        """取得未讀家長提問數量。"""
        return (
            session.query(func.count(ParentInquiry.id))
            .filter(ParentInquiry.is_read.is_(False))
            .scalar() or 0
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
    ) -> int:
        """計算指定課程的有效報名數。"""
        query = self._active_course_query(session, course_id)
        if status is not None:
            query = query.filter(RegistrationCourse.status == status)
        return query.count()

    # ------------------------------------------------------------------ #
    # 統計儀表板
    # ------------------------------------------------------------------ #

    def get_stats(self, session) -> dict:
        """取得統計摘要（含報名趨勢、熱門課程）"""
        today = date.today()

        total_registrations = (
            session.query(func.count(ActivityRegistration.id))
            .filter(ActivityRegistration.is_active.is_(True))
            .scalar() or 0
        )

        total_enrollments = (
            session.query(func.count(RegistrationCourse.id))
            .join(ActivityRegistration, RegistrationCourse.registration_id == ActivityRegistration.id)
            .filter(
                RegistrationCourse.status == "enrolled",
                ActivityRegistration.is_active.is_(True),
            )
            .scalar() or 0
        )

        total_waitlist = (
            session.query(func.count(RegistrationCourse.id))
            .join(ActivityRegistration, RegistrationCourse.registration_id == ActivityRegistration.id)
            .filter(
                RegistrationCourse.status == "waitlist",
                ActivityRegistration.is_active.is_(True),
            )
            .scalar() or 0
        )

        total_supply_orders = (
            session.query(func.count(RegistrationSupply.id))
            .join(ActivityRegistration, RegistrationSupply.registration_id == ActivityRegistration.id)
            .filter(ActivityRegistration.is_active.is_(True))
            .scalar() or 0
        )

        today_new = (
            session.query(func.count(ActivityRegistration.id))
            .filter(
                ActivityRegistration.is_active.is_(True),
                func.date(ActivityRegistration.created_at) == today,
            )
            .scalar() or 0
        )

        # 已繳費收入
        paid_revenue_courses = (
            session.query(func.coalesce(func.sum(RegistrationCourse.price_snapshot), 0))
            .join(ActivityRegistration, RegistrationCourse.registration_id == ActivityRegistration.id)
            .filter(
                ActivityRegistration.is_paid.is_(True),
                ActivityRegistration.is_active.is_(True),
                RegistrationCourse.status == "enrolled",
            )
            .scalar() or 0
        )
        paid_revenue_supplies = (
            session.query(func.coalesce(func.sum(RegistrationSupply.price_snapshot), 0))
            .join(ActivityRegistration, RegistrationSupply.registration_id == ActivityRegistration.id)
            .filter(
                ActivityRegistration.is_paid.is_(True),
                ActivityRegistration.is_active.is_(True),
            )
            .scalar() or 0
        )

        # 未繳費金額
        unpaid_revenue_courses = (
            session.query(func.coalesce(func.sum(RegistrationCourse.price_snapshot), 0))
            .join(ActivityRegistration, RegistrationCourse.registration_id == ActivityRegistration.id)
            .filter(
                ActivityRegistration.is_paid.is_(False),
                ActivityRegistration.is_active.is_(True),
                RegistrationCourse.status == "enrolled",
            )
            .scalar() or 0
        )
        unpaid_revenue_supplies = (
            session.query(func.coalesce(func.sum(RegistrationSupply.price_snapshot), 0))
            .join(ActivityRegistration, RegistrationSupply.registration_id == ActivityRegistration.id)
            .filter(
                ActivityRegistration.is_paid.is_(False),
                ActivityRegistration.is_active.is_(True),
            )
            .scalar() or 0
        )

        total_capacity = (
            session.query(func.coalesce(func.sum(ActivityCourse.capacity), 0))
            .filter(ActivityCourse.is_active.is_(True))
            .scalar() or 0
        )
        enrollment_rate = (
            round(total_enrollments / total_capacity * 100, 1)
            if total_capacity > 0 else 0.0
        )

        unread_inquiries = self.get_unread_inquiries_count(session)

        # 每日報名趨勢（最近 30 筆不同日期）
        daily_rows = (
            session.query(
                func.date(ActivityRegistration.created_at).label("d"),
                func.count(ActivityRegistration.id).label("c"),
            )
            .filter(ActivityRegistration.is_active.is_(True))
            .group_by(func.date(ActivityRegistration.created_at))
            .order_by(func.date(ActivityRegistration.created_at))
            .all()
        )
        daily_stats = [{"date": str(row.d), "count": row.c} for row in daily_rows]

        # 熱門課程（top 5）
        top_courses_rows = (
            session.query(
                ActivityCourse.name,
                func.count(RegistrationCourse.id).label("c"),
            )
            .join(RegistrationCourse, ActivityCourse.id == RegistrationCourse.course_id)
            .join(ActivityRegistration, RegistrationCourse.registration_id == ActivityRegistration.id)
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
            "statistics": {
                "totalRegistrations": total_registrations,
                "totalEnrollments": total_enrollments,
                "totalWaitlist": total_waitlist,
                "totalSupplyOrders": total_supply_orders,
                "todayNewRegistrations": today_new,
                "totalRevenue": int(paid_revenue_courses) + int(paid_revenue_supplies),
                "totalUnpaid": int(unpaid_revenue_courses) + int(unpaid_revenue_supplies),
                "enrollmentRate": enrollment_rate,
                "unreadInquiries": unread_inquiries,
            },
            "charts": {
                "daily": daily_stats,
                "topCourses": top_courses,
            },
        }

    # ------------------------------------------------------------------ #
    # 統計儀表板表格 (依班級)
    # ------------------------------------------------------------------ #

    def get_dashboard_table(self, session) -> dict:
        """取得課後才藝儀表板統計表格（含依班級與年級的小計與達成率）"""
        from models.classroom import Classroom, ClassGrade, Student
        from models.employee import Employee

        # 1. 取得所有開放的課程
        courses = session.query(ActivityCourse).filter(ActivityCourse.is_active.is_(True)).order_by(ActivityCourse.id).all()
        course_list = [{"id": c.id, "name": c.name} for c in courses]

        # 2. 取得所有年級
        grades = session.query(ClassGrade).filter(ClassGrade.is_active.is_(True)).order_by(ClassGrade.sort_order).all()

        target_mapping = {
            "大班": 100,
            "中班": 90,
            "小班": 80,
            "幼娃班": 70,
            "娃娃班": 70,
            "幼幼班": 70,
            "幼班": 70
        }

        # 3. 抓取每個班級的在籍人數
        classroom_students = (
            session.query(
                Classroom.id,
                func.count(Student.id).label("student_count")
            )
            .outerjoin(Student, (Student.classroom_id == Classroom.id) & (Student.is_active.is_(True)))
            .filter(Classroom.is_active.is_(True))
            .group_by(Classroom.id)
            .all()
        )
        student_count_map = {row.id: row.student_count for row in classroom_students}

        # 4. 抓取每個班級、每個課程的報名人數
        enrollments = (
            session.query(
                ActivityRegistration.class_name,
                RegistrationCourse.course_id,
                func.count(RegistrationCourse.id).label("count")
            )
            .join(ActivityRegistration, RegistrationCourse.registration_id == ActivityRegistration.id)
            .filter(
                ActivityRegistration.is_active.is_(True),
                RegistrationCourse.status == "enrolled"
            )
            .group_by(ActivityRegistration.class_name, RegistrationCourse.course_id)
            .all()
        )
        enrollment_map = {}
        for row in enrollments:
            enrollment_map[(row.class_name, row.course_id)] = row.count

        # 5. 組裝報表結構
        result_grades = []
        
        gt_student_count = 0
        gt_courses = {str(c.id): 0 for c in courses}
        gt_total_enrollments = 0

        for grade in grades:
            grade_name = grade.name
            target_pct = 0
            for k, v in target_mapping.items():
                if k in grade_name:
                    target_pct = v
                    break

            # 找出這年級所有的班級與班導師
            classrooms_data = (
                session.query(Classroom, Employee.name.label("teacher_name"))
                .outerjoin(Employee, Classroom.head_teacher_id == Employee.id)
                .filter(
                    Classroom.grade_id == grade.id,
                    Classroom.is_active.is_(True)
                )
                .all()
            )

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
                    count = enrollment_map.get((cls.name, c.id), 0)
                    cls_course_data[c_id_str] = count
                    cls_enrollments += count

                    sub_courses[c_id_str] += count
                    gt_courses[c_id_str] += count

                cls_ratio = int(round(cls_enrollments / cls_student_count * 100)) if cls_student_count > 0 else 0

                sub_student_count += cls_student_count
                sub_total_enrollments += cls_enrollments

                gt_student_count += cls_student_count
                gt_total_enrollments += cls_enrollments

                classroom_list.append({
                    "classroom_id": cls.id,
                    "classroom_name": cls.name,
                    "teacher_name": teacher_name or "",
                    "student_count": cls_student_count,
                    "courses": cls_course_data,
                    "total_enrollments": cls_enrollments,
                    "ratio": cls_ratio,
                })

            # 計算年級小計
            sub_ratio = int(round(sub_total_enrollments / sub_student_count * 100)) if sub_student_count > 0 else 0

            sub_bonus = 0
            sub_points = 0
            if target_pct > 0 and sub_ratio >= target_pct:
                sub_bonus = 1000
                sub_points = target_pct

            result_grades.append({
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
                }
            })

        gt_ratio = int(round(gt_total_enrollments / gt_student_count * 100)) if gt_student_count > 0 else 0
        grand_total = {
            "student_count": gt_student_count,
            "courses": gt_courses,
            "total_enrollments": gt_total_enrollments,
            "ratio": gt_ratio,
        }

        return {
            "courses": course_list,
            "grades": result_grades,
            "grand_total": grand_total
        }

    # ------------------------------------------------------------------ #
    # 候補升正式
    # ------------------------------------------------------------------ #

    def promote_waitlist(self, session, registration_id: int, course_id: int) -> bool:
        """
        將指定報名的指定課程從候補升為正式。
        使用 with_for_update() 防止並發超額。
        回傳 True 成功，ValueError 失敗。
        """
        rc = (
            session.query(RegistrationCourse)
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                RegistrationCourse.registration_id == registration_id,
                RegistrationCourse.course_id == course_id,
                RegistrationCourse.status == "waitlist",
                ActivityRegistration.is_active.is_(True),
            )
            .with_for_update()
            .first()
        )
        if not rc:
            raise ValueError("報名課程項目不存在或非候補狀態")

        enrolled_count = self.count_active_course_registrations(
            session,
            course_id,
            status="enrolled",
        )
        course = session.query(ActivityCourse).filter(ActivityCourse.id == course_id).first()
        if not course:
            raise ValueError("課程不存在")

        if course.capacity is not None and enrolled_count >= course.capacity:
            raise ValueError("課程容量已滿，無法升為正式")

        rc.status = "enrolled"
        return True

    # ------------------------------------------------------------------ #
    # 軟刪除報名
    # ------------------------------------------------------------------ #

    def delete_registration(self, session, registration_id: int, operator: str):
        """軟刪除報名，並記錄修改紀錄"""
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

        reg.is_active = False
        self.log_change(
            session, registration_id, reg.student_name,
            "刪除報名", "管理員刪除報名", operator,
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
        """回傳 (capacity, enrolled_count, has_vacancy)"""
        course = session.query(ActivityCourse).filter(ActivityCourse.id == course_id).first()
        if not course:
            raise ValueError("課程不存在")

        enrolled_count = (
            session.query(func.count(RegistrationCourse.id))
            .filter(
                RegistrationCourse.course_id == course_id,
                RegistrationCourse.status == "enrolled",
            )
            .scalar() or 0
        )
        capacity = course.capacity if course.capacity is not None else 999
        has_vacancy = enrolled_count < capacity
        return capacity, enrolled_count, has_vacancy


# Module-level singleton
activity_service = ActivityService()
