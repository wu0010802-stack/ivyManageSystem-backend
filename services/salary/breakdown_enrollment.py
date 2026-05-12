"""薪資頁學生人數展開區用 helper。

不改動 engine 與 student_enrollment.classroom_student_count_map 簽名；本檔僅讀資料組裝
給 records.py 列表 dict 使用。
"""

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from models.classroom import Classroom
from services.student_enrollment import count_students_active_on


def compute_enrollment_breakdown(
    session: Session,
    employee_id: int,
    target_date: date,
) -> Optional[dict]:
    """Return enrollment + assistant breakdown for an employee at target_date.

    Returns None if employee teaches no active classroom (neither head nor
    assistant nor art). Otherwise the dict has shape:

        {
            "enrollment": { snapshot_date, total, classroom_id, classroom_name, grade_name } | None,
            "assistant":  { by_classroom: [str, ...] } | None,
        }

    Note: this helper issues up to 3 small queries per call (one for head
    classroom + grade, one for student count, one for assistant classrooms).
    For batch use cases (e.g. salary records list iterating up to 500 rows),
    callers should preload `Classroom` rows and `classroom_student_count_map`
    once per request and adapt to a batch shape; see
    services/salary/engine.py:3093-3108 for the existing preload pattern.
    """
    head_classroom = (
        session.query(Classroom)
        .options(joinedload(Classroom.grade))
        .filter(
            Classroom.head_teacher_id == employee_id,
            Classroom.is_active.is_(True),
        )
        .order_by(Classroom.id)
        .first()
    )

    enrollment = None
    if head_classroom is not None:
        total = count_students_active_on(session, target_date, head_classroom.id)
        grade_name = head_classroom.grade.name if head_classroom.grade else None
        enrollment = {
            "snapshot_date": target_date.isoformat(),
            "total": total,
            "classroom_id": head_classroom.id,
            "classroom_name": head_classroom.name,
            "grade_name": grade_name,
        }

    head_classroom_id = head_classroom.id if head_classroom else None
    assistant_classrooms = (
        session.query(Classroom)
        .filter(
            Classroom.is_active.is_(True),
            (
                (Classroom.assistant_teacher_id == employee_id)
                | (Classroom.art_teacher_id == employee_id)
            ),
        )
        .order_by(Classroom.id)
        .all()
    )
    assistant_names = [
        c.name for c in assistant_classrooms if c.id != head_classroom_id
    ]

    assistant = {"by_classroom": assistant_names} if assistant_names else None

    if enrollment is None and assistant is None:
        return None
    return {"enrollment": enrollment, "assistant": assistant}
