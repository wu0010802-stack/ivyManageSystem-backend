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
    """回 (start_count, end_count, end_count_is_live)。"""
    start_date, end_date = term_bounds(school_year, semester)
    start_count = _snapshot_count(
        session, classroom_id, start_date.year, start_date.month
    )
    if is_current:
        end_count = count_students_active_on(session, today_taipei(), classroom_id)
        return start_count, end_count, True
    end_count = _snapshot_count(session, classroom_id, end_date.year, end_date.month)
    return start_count, end_count, False


def build_class_history(session, employee_id: int) -> list[dict]:
    """（Task 3 實作）回傳員工所有任班學期歷程。"""
    raise NotImplementedError("build_class_history is implemented in Task 3")
