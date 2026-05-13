"""薪資頁學生人數展開區用 helper。

不改動 engine 與 student_enrollment.classroom_student_count_map 簽名；本檔僅讀資料組裝
給 records.py 列表 dict 使用。
"""

import logging
from datetime import date
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from models.classroom import Classroom
from services.student_enrollment import count_students_active_on
from utils.academic import resolve_current_academic_term

logger = logging.getLogger(__name__)


def compute_enrollment_breakdown(
    session: Session,
    employee_id: int,
    target_date: date,
) -> Optional[dict]:
    """Return enrollment + assistant breakdown for an employee at target_date.

    Returns None if employee teaches no active classroom (neither head nor
    assistant nor art). Otherwise the dict has shape:

        {
            "enrollment": { snapshot_date, total, classroom_id, classroom_name,
                            grade_name, multi_head } | None,
            "assistant":  { by_classroom: [str, ...] } | None,
        }

    班級反查：依 target_date 解析學年度/學期，先取「當期」班級；若該員工當期
    無對應 active 班級，fallback 至跨期任一 active 並 log warning（與
    services/salary/engine.py:_resolve_classroom_for_employee_in_term 同行為）。

    多頭班級：若同一 employee 同時為多個 active 班級的 head_teacher，回傳第一
    個（id 升冪），但在 enrollment dict 內補 multi_head=True 旗標讓前端揭露；
    並 log warning 方便管理員修資料。

    Note: this helper issues up to 3 small queries per call (one for head
    classroom + grade, one for student count, one for assistant classrooms).
    For batch use cases (e.g. salary records list iterating up to 500 rows),
    callers should preload `Classroom` rows and `classroom_student_count_map`
    once per request and adapt to a batch shape; see
    services/salary/engine.py:3093-3108 for the existing preload pattern.
    """
    school_year, semester = resolve_current_academic_term(target_date)

    head_base_q = (
        session.query(Classroom)
        .options(joinedload(Classroom.grade))
        .filter(
            Classroom.head_teacher_id == employee_id,
            Classroom.is_active.is_(True),
        )
    )

    head_term_classrooms = (
        head_base_q.filter(
            Classroom.school_year == school_year,
            Classroom.semester == semester,
        )
        .order_by(Classroom.id.asc())
        .all()
    )

    if head_term_classrooms:
        head_classrooms = head_term_classrooms
    else:
        head_classrooms = head_base_q.order_by(Classroom.id.asc()).all()
        if head_classrooms:
            logger.warning(
                "員工 %s 在 school_year=%s semester=%s 無對應 active head 班級；"
                "fallback 使用 classroom_id=%s",
                employee_id,
                school_year,
                semester,
                head_classrooms[0].id,
            )

    head_classroom = head_classrooms[0] if head_classrooms else None
    multi_head = len(head_classrooms) > 1
    if multi_head:
        logger.warning(
            "員工 %s 同時為多個 active 班級的 head_teacher（ids=%s）；breakdown "
            "僅顯示第一個並標 multi_head=True",
            employee_id,
            [c.id for c in head_classrooms],
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
            "multi_head": multi_head,
        }

    head_classroom_id = head_classroom.id if head_classroom else None
    assistant_base_q = session.query(Classroom).filter(
        Classroom.is_active.is_(True),
        or_(
            Classroom.assistant_teacher_id == employee_id,
            Classroom.art_teacher_id == employee_id,
        ),
    )
    assistant_term_classrooms = (
        assistant_base_q.filter(
            Classroom.school_year == school_year,
            Classroom.semester == semester,
        )
        .order_by(Classroom.id)
        .all()
    )
    if assistant_term_classrooms:
        assistant_classrooms = assistant_term_classrooms
    else:
        assistant_classrooms = assistant_base_q.order_by(Classroom.id).all()

    assistant_names = [
        c.name for c in assistant_classrooms if c.id != head_classroom_id
    ]

    assistant = {"by_classroom": assistant_names} if assistant_names else None

    if enrollment is None and assistant is None:
        return None
    return {"enrollment": enrollment, "assistant": assistant}
