"""學生在籍判斷共用 helper。"""

from __future__ import annotations

from datetime import date

from sqlalchemy import and_, func, or_

from models.database import Student


def student_active_on_filter(target_date: date):
    """回傳指定日期視角的學生在籍 SQL 條件。"""
    return and_(
        or_(Student.enrollment_date.is_(None), Student.enrollment_date <= target_date),
        or_(Student.graduation_date.is_(None), Student.graduation_date >= target_date),
    )


def count_students_active_on(session, target_date: date, classroom_id: int | None = None) -> int:
    query = session.query(func.count(Student.id)).filter(student_active_on_filter(target_date))
    if classroom_id is not None:
        query = query.filter(Student.classroom_id == classroom_id)
    return int(query.scalar() or 0)


def classroom_student_count_map(session, target_date: date) -> dict[int, int]:
    rows = (
        session.query(Student.classroom_id, func.count(Student.id))
        .filter(student_active_on_filter(target_date))
        .group_by(Student.classroom_id)
        .all()
    )
    return {classroom_id: int(count or 0) for classroom_id, count in rows if classroom_id is not None}
