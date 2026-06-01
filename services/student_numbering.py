"""services/student_numbering.py — 學生永久編號配發 + 學號顯示快取組字。

身分認定鍵 = (Student.enrollment_school_year, Student.enrollment_seq)，永久不變。
對外 student_id 是「由當前班級 + 永久 seq 組出的顯示快取」，由 before_flush
listener（models/student_events.py）維護。本模組只提供純函式 + 配發器。
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, text
from sqlalchemy.orm import Session

_LOCK_NS_ENROLLMENT = 1001  # 固定整數 advisory lock namespace（跨 process 穩定）


def grade_char(grade_name: Optional[str]) -> str:
    """年級名稱首字：大班→大、中班→中、小班→小、幼幼班→幼；空白回 ''。"""
    return (grade_name or "").strip()[:1]


def compute_student_display_id(session: Session, student) -> Optional[str]:
    """組出學號顯示快取。

    - 有班級且有年級： {classroom.school_year}-{年級字}-{seq:02d}
    - 有班級無年級：   {classroom.school_year}-{seq:02d}
    - 無班級：         {enrollment_school_year}-{seq:02d}
    - enrollment_seq 為 None（legacy/未配號）：原樣回傳 student.student_id（不接管）
    """
    from models.classroom import Classroom, ClassGrade

    seq = student.enrollment_seq
    if seq is None:
        return student.student_id

    classroom = (
        session.get(Classroom, student.classroom_id) if student.classroom_id else None
    )
    if classroom is not None:
        gname = None
        if classroom.grade_id:
            grade = session.get(ClassGrade, classroom.grade_id)
            gname = grade.name if grade else None
        gc = grade_char(gname)
        if gc:
            return f"{classroom.school_year}-{gc}-{seq:02d}"
        return f"{classroom.school_year}-{seq:02d}"

    year = student.enrollment_school_year
    return f"{year}-{seq:02d}" if year is not None else f"{seq:02d}"


def next_enrollment_seq(session: Session, school_year: int) -> int:
    """配發指定發號學年的下一個永久 seq（該學年內 max+1）。

    Postgres 上以 pg_advisory_xact_lock 防並發撞號（雙參數整數形式，跨 process 穩定）。
    SQLite/其他 dialect 跳過 lock。
    """
    from models.classroom import Student

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :year)"),
            {"ns": _LOCK_NS_ENROLLMENT, "year": int(school_year)},
        )

    max_seq = (
        session.query(func.max(Student.enrollment_seq))
        .filter(Student.enrollment_school_year == school_year)
        .scalar()
    )
    return (max_seq or 0) + 1
