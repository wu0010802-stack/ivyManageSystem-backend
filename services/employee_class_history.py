"""員工班級歷程查詢 service。

資歷主幹（學期×班級×角色×同班搭檔）從 Classroom 的 teacher FK 反查，可靠。
人數為資訊等級：過去學期讀 MonthlyEnrollmentSnapshot 當期快照；當前學期班
期末用即時在籍數。無快照即 None，不做會算錯的轉班回放（見設計 spec §6）。
"""

from __future__ import annotations

from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload

from models.database import Classroom, Employee
from models.gov_moe import MonthlyEnrollmentSnapshot
from services.student_enrollment import count_students_active_on
from utils.academic import resolve_current_academic_term, term_bounds
from utils.taipei_time import today_taipei


def _snapshot_count(session, classroom_id: int, year: int, month: int) -> int | None:
    """某班某西元年月的快照人數（跨 age_group 加總）；無資料回 None。"""
    total = (
        session.query(func.sum(MonthlyEnrollmentSnapshot.total_count))
        .filter(
            MonthlyEnrollmentSnapshot.classroom_id == classroom_id,
            MonthlyEnrollmentSnapshot.year == year,
            MonthlyEnrollmentSnapshot.month == month,
        )
        .scalar()
    )
    return int(total) if total is not None else None


def _term_headcounts(
    session, classroom_id: int, school_year: int, semester: int, is_current: bool
) -> tuple[int | None, int | None, bool]:
    """回 (start_count, end_count, end_count_is_live)。

    semester 必須是 1 或 2（term_bounds 對其他值會 raise ValueError）；
    end_count 在 is_current 時為即時在籍數（永不 None），快照路徑才可能 None。
    """
    start_date, end_date = term_bounds(school_year, semester)
    start_count = _snapshot_count(
        session, classroom_id, start_date.year, start_date.month
    )
    if is_current:
        end_count = count_students_active_on(session, today_taipei(), classroom_id)
        return start_count, end_count, True
    end_count = _snapshot_count(session, classroom_id, end_date.year, end_date.month)
    return start_count, end_count, False


def _resolve_role(classroom: Classroom, employee_id: int) -> str | None:
    """員工在這班的角色：head 優先；art 不成列（回 None）。"""
    if classroom.head_teacher_id == employee_id:
        return "head"
    if classroom.assistant_teacher_id == employee_id:
        return "assistant"
    return None


def build_class_history(session, employee_id: int) -> list[dict]:
    """回該員工的班級歷程列（dict，對齊 ClassHistoryRow shape）。"""
    classrooms = (
        session.query(Classroom)
        .options(joinedload(Classroom.grade))
        .filter(
            or_(
                Classroom.head_teacher_id == employee_id,
                Classroom.assistant_teacher_id == employee_id,
            )
        )
        .order_by(Classroom.school_year.desc(), Classroom.semester.desc())
        .all()
    )
    if not classrooms:
        return []

    # 批次解析所有搭檔姓名，避免 N+1
    teacher_ids: set[int] = set()
    for c in classrooms:
        for tid in (c.head_teacher_id, c.assistant_teacher_id, c.art_teacher_id):
            if tid is not None and tid != employee_id:
                teacher_ids.add(tid)
    name_map: dict[int, str] = {}
    if teacher_ids:
        for emp_id, emp_name in (
            session.query(Employee.id, Employee.name)
            .filter(Employee.id.in_(teacher_ids))
            .all()
        ):
            name_map[emp_id] = emp_name

    cur_year, cur_sem = resolve_current_academic_term()

    rows: list[dict] = []
    for c in classrooms:
        role = _resolve_role(c, employee_id)
        if role is None:
            continue
        is_current = c.school_year == cur_year and c.semester == cur_sem
        start_count, end_count, end_is_live = _term_headcounts(
            session, c.id, c.school_year, c.semester, is_current
        )
        net_change = (
            end_count - start_count
            if start_count is not None and end_count is not None
            else None
        )
        co_teachers = []
        for tid, trole in (
            (c.head_teacher_id, "head"),
            (c.assistant_teacher_id, "assistant"),
            (c.art_teacher_id, "art"),
        ):
            if tid is None or tid == employee_id:
                continue
            co_teachers.append(
                {"role": trole, "employee_id": tid, "name": name_map.get(tid, "")}
            )
        rows.append(
            {
                "school_year": c.school_year,
                "semester": c.semester,
                "classroom_id": c.id,
                "classroom_name": c.name,
                "grade_name": c.grade.name if c.grade else None,
                "role": role,
                "co_teachers": co_teachers,
                "is_current": is_current,
                "start_count": start_count,
                "end_count": end_count,
                "end_count_is_live": end_is_live,
                "net_change": net_change,
            }
        )
    return rows
