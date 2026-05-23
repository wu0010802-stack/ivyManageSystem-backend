"""services/recruitment_lifecycle.py — scheduler 批量推進入口。

依 academic_terms.start_date 將 window 內的 enrolled 學生升 active。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from models.academic_term import AcademicTerm
from models.classroom import Student, LIFECYCLE_ENROLLED
from services.recruitment_funnel import (
    transition_visit,
    RecruitmentFunnelError,
)

logger = logging.getLogger(__name__)


def advance_term_to_active(
    session: Session,
    school_year: int,
    semester: int,
) -> dict:
    """把該學期 window 內的 enrolled 學生升 active。

    Window: [term.start_date - window_days, term.start_date]，
    window_days 由 settings.scheduler.recruitment_term_advance_window_days 控（預設 90）。

    Returns: {"advanced": N, "skipped": M, "failed": K}
    """
    term = (
        session.query(AcademicTerm)
        .filter(
            AcademicTerm.school_year == school_year,
            AcademicTerm.semester == semester,
        )
        .first()
    )
    if term is None:
        logger.warning(
            "academic_terms not found: year=%s sem=%s", school_year, semester
        )
        return {"advanced": 0, "skipped": 0, "failed": 0}

    # 從 settings 拿 window；testing 環境若拿不到，預設 90
    try:
        from config import get_settings

        window_days = get_settings().scheduler.recruitment_term_advance_window_days
    except (AttributeError, Exception):
        window_days = 90

    window_start = term.start_date - timedelta(days=window_days)

    students = (
        session.query(Student)
        .filter(
            Student.recruitment_visit_id.isnot(None),
            Student.enrollment_date.isnot(None),
            Student.enrollment_date >= window_start,
            Student.enrollment_date <= term.start_date,
            Student.lifecycle_status == LIFECYCLE_ENROLLED,
        )
        .all()
    )

    advanced = 0
    skipped = 0
    failed = 0
    for stu in students:
        try:
            transition_visit(
                session,
                visit_id=stu.recruitment_visit_id,
                to_stage="active",
                actor_user_id=None,
                reason="scheduler:term_start",
            )
            advanced += 1
        except RecruitmentFunnelError as e:
            if e.code == "STAGE_ALREADY":
                skipped += 1
            else:
                failed += 1
                logger.warning(
                    "advance failed student=%s code=%s: %s",
                    stu.id,
                    e.code,
                    e,
                )
        except Exception:
            failed += 1
            logger.exception("advance error student=%s", stu.id)

    return {"advanced": advanced, "skipped": skipped, "failed": failed}
