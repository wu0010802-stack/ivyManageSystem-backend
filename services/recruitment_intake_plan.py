"""services/recruitment_intake_plan.py — 新生名額規劃。

職責：
- compute_intake_plan(): 以「目標學年 × 學期」彙總每年級 計畫/保留/註冊/剩餘。
- set_provisional_seat(): 設定/釋放某訪視的暫定編班（保留座位）。
- upsert_intake_targets(): 設定計畫名額。

名額計算單一真相（spec §7）：
- reserved = recruitment_visits 有 provisional_grade_id 且 enrolled=False。
- enrolled = Student join 其 recruitment_visit（visit.provisional_grade_id 為年級歸屬），
  Student.enrollment_school_year=目標學年 且 lifecycle 非終態。
兩集合以 enrolled 旗標互斥，無重複計數。
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.classroom import (
    ClassGrade,
    Student,
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
)
from models.recruitment import GradeIntakeTarget, RecruitmentVisit

_TERMINAL = (LIFECYCLE_GRADUATED, LIFECYCLE_TRANSFERRED, LIFECYCLE_WITHDRAWN)


class IntakePlanError(ValueError):
    """名額規劃業務錯誤。"""


def compute_intake_plan(
    session: Session, *, school_year: int, semester: int = 1
) -> list[dict]:
    """回傳每個 active 年級一列：target / reserved / enrolled / remaining / over_capacity。"""
    # reserved：未轉換的保留 visit，依 provisional_grade_id 分組
    reserved_rows = (
        session.query(
            RecruitmentVisit.provisional_grade_id, func.count(RecruitmentVisit.id)
        )
        .filter(
            RecruitmentVisit.provisional_grade_id.isnot(None),
            RecruitmentVisit.target_school_year == school_year,
            RecruitmentVisit.target_semester == semester,
            RecruitmentVisit.enrolled.is_(False),
        )
        .group_by(RecruitmentVisit.provisional_grade_id)
        .all()
    )
    reserved_by_grade = {gid: cnt for gid, cnt in reserved_rows}

    # enrolled：Student join visit；以 visit.provisional_grade_id 為年級歸屬
    enrolled_rows = (
        session.query(RecruitmentVisit.provisional_grade_id, func.count(Student.id))
        .join(Student, Student.recruitment_visit_id == RecruitmentVisit.id)
        .filter(
            RecruitmentVisit.provisional_grade_id.isnot(None),
            RecruitmentVisit.target_semester == semester,
            Student.enrollment_school_year == school_year,
            Student.lifecycle_status.notin_(_TERMINAL),
        )
        .group_by(RecruitmentVisit.provisional_grade_id)
        .all()
    )
    enrolled_by_grade = {gid: cnt for gid, cnt in enrolled_rows}

    target_rows = (
        session.query(GradeIntakeTarget.grade_id, GradeIntakeTarget.target_seats)
        .filter(
            GradeIntakeTarget.school_year == school_year,
            GradeIntakeTarget.semester == semester,
        )
        .all()
    )
    target_by_grade = {gid: seats for gid, seats in target_rows}

    grades = (
        session.query(ClassGrade)
        .filter(ClassGrade.is_active.is_(True))
        .order_by(ClassGrade.sort_order, ClassGrade.id)
        .all()
    )

    rows: list[dict] = []
    for g in grades:
        reserved = int(reserved_by_grade.get(g.id, 0))
        enrolled = int(enrolled_by_grade.get(g.id, 0))
        target = int(target_by_grade.get(g.id, 0))
        rows.append(
            {
                "grade_id": g.id,
                "grade_name": g.name,
                "target_seats": target,
                "reserved_count": reserved,
                "enrolled_count": enrolled,
                "remaining": target - reserved - enrolled,
                "over_capacity": (reserved + enrolled) > target,
            }
        )
    return rows
