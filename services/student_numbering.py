"""services/student_numbering.py — 學生永久編號配發 + 學號顯示快取組字。

身分認定鍵 = (Student.enrollment_school_year, Student.enrollment_seq)，永久不變。
對外 student_id 是「由當前班級 + 永久 seq 組出的顯示快取」，由 before_flush
listener（models/student_events.py）維護。本模組只提供純函式 + 配發器。
"""

from __future__ import annotations

import re
from datetime import date
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


_OLD_STUDENT_ID_PREFIX_RE = re.compile(r"^(\d{3})-")


def _roc_year_from_date(d: date) -> int | None:
    """日期 → 學年（民國）：8 月起算當學年，否則前一學年。"""
    if d is None:
        return None
    base = d.year if d.month >= 8 else d.year - 1
    return base - 1911


def backfill_enrollment_numbers(session: Session) -> int:
    """為既有學生回填 enrollment_school_year + enrollment_seq（冪等）。

    - 已有 enrollment_seq 的列跳過（冪等）。
    - 發號學年：解析 student_id 前綴 ^(\\d{3})- → 失敗用 enrollment_date 推算 →
      再失敗用當前學年。
    - seq：在每個發號學年內，依 id 排序對「尚未配號」者接續 max(現有 seq)+1。
    - 同時重算 student_id 顯示快取（listener 不會對 bulk-loaded 既有列觸發）。

    回傳處理筆數。
    """
    from models.classroom import Student
    from utils.academic import resolve_current_academic_term

    cur_year, _ = resolve_current_academic_term()

    pending = (
        session.query(Student)
        .filter(Student.enrollment_seq.is_(None))
        .order_by(Student.id)
        .all()
    )
    if not pending:
        return 0

    year_max: dict[int, int] = {}
    for yr, mx in (
        session.query(Student.enrollment_school_year, func.max(Student.enrollment_seq))
        .filter(Student.enrollment_seq.isnot(None))
        .group_by(Student.enrollment_school_year)
        .all()
    ):
        if yr is not None:
            year_max[yr] = mx or 0

    count = 0
    for stu in pending:
        year = None
        m = _OLD_STUDENT_ID_PREFIX_RE.match(stu.student_id or "")
        if m:
            year = int(m.group(1))
        if year is None:
            year = _roc_year_from_date(stu.enrollment_date)
        if year is None:
            year = cur_year

        nxt = year_max.get(year, 0) + 1
        year_max[year] = nxt
        stu.enrollment_school_year = year
        stu.enrollment_seq = nxt
        # listener 不會對 bulk-loaded 既有列觸發 → 此處直接重算 student_id 顯示快取
        stu.student_id = compute_student_display_id(session, stu) or stu.student_id
        count += 1

    return count
