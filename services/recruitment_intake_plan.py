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
from models.recruitment import (
    GradeIntakeTarget,
    RecruitmentEventLog,
    RecruitmentVisit,
)
from utils.taipei_time import now_taipei_naive

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
            # 反轉移除預繳後仍掛保留欄位的 visit 不算進 reserved（spec §9）
            RecruitmentVisit.has_deposit.is_(True),
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
            # R4-5：enrolled 與 reserved 用同一年鍵（visit.target_school_year），避免
            # 轉換後改 target_school_year 時同一人在某年算 enrolled、另一年算 reserved。
            RecruitmentVisit.target_school_year == school_year,
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


def set_provisional_seat(
    session: Session,
    *,
    visit_id: int,
    provisional_grade_id: Optional[int],
    target_school_year: Optional[int],
    target_semester: Optional[int],
    actor_user_id: Optional[int],
) -> RecruitmentVisit:
    """設定（provisional_grade_id 非 None）或釋放（None）某訪視的保留座位。

    守衛：設定時 visit.has_deposit 必須為 True。寫 recruitment_event_log。
    Commit 由 caller 負責。
    """
    visit = session.query(RecruitmentVisit).filter_by(id=visit_id).first()
    if visit is None:
        raise IntakePlanError(f"招生訪視不存在：id={visit_id}")

    is_set = provisional_grade_id is not None
    if is_set and not visit.has_deposit:
        raise IntakePlanError("未預繳的訪視不可保留座位")
    if is_set and target_school_year is None:
        raise IntakePlanError("保留座位需指定目標學年")

    # service 自洽：設定時若未給 target_semester 預設為 1（不依賴 caller 補）
    resolved_semester = (
        (target_semester if target_semester is not None else 1) if is_set else None
    )
    visit.provisional_grade_id = provisional_grade_id
    visit.target_school_year = target_school_year
    visit.target_semester = resolved_semester

    log = RecruitmentEventLog(
        recruitment_visit_id=visit.id,
        event_type="seat_reserved" if is_set else "seat_released",
        from_stage="deposited",
        to_stage="deposited",
        actor_user_id=actor_user_id,
        metadata_json={
            "grade_id": provisional_grade_id,
            "school_year": target_school_year,
            "semester": resolved_semester,
        },
        created_at=now_taipei_naive(),
    )
    session.add(log)
    session.flush()
    return visit


def upsert_intake_targets(
    session: Session,
    *,
    school_year: int,
    semester: int,
    targets: list[dict],
) -> list[GradeIntakeTarget]:
    """以 (grade_id, school_year, semester) upsert 計畫名額。Commit 由 caller 負責。"""
    result: list[GradeIntakeTarget] = []
    for item in targets:
        gid = int(item["grade_id"])
        seats = int(item["target_seats"])
        row = (
            session.query(GradeIntakeTarget)
            .filter_by(grade_id=gid, school_year=school_year, semester=semester)
            .first()
        )
        if row is None:
            row = GradeIntakeTarget(
                grade_id=gid,
                school_year=school_year,
                semester=semester,
                target_seats=seats,
            )
            session.add(row)
        else:
            row.target_seats = seats
        result.append(row)
    session.flush()
    return result
