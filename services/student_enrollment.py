"""學生在籍判斷共用 helper。"""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import and_, func, or_

from models.database import Student


def student_active_on_filter(target_date: date):
    """回傳指定日期視角的學生在籍 SQL 條件。

    withdrawal_date 條件（2026-06-13 L1a）：轉出/退學流程只設 withdrawal_date
    不設 graduation_date（services/student_lifecycle.transition），修正前退學生
    會永遠灌水在籍人數。邊界對齊 year_end/enrollment_rates._enrolled_on_filter：
    withdrawal_date 當日即不在籍（> target_date 才算在籍）。
    """
    return and_(
        or_(Student.enrollment_date.is_(None), Student.enrollment_date <= target_date),
        or_(Student.graduation_date.is_(None), Student.graduation_date >= target_date),
        or_(Student.withdrawal_date.is_(None), Student.withdrawal_date > target_date),
    )


def count_students_active_on(
    session, target_date: date, classroom_id: int | None = None
) -> int:
    """全校（或指定班）在籍人數。

    注意：classroom_id 過濾用學生「現態」歸屬，僅適合 target_date=今天的現況
    查詢（production 唯一站點 employee_class_history 即此用法）。歷史日期的
    班級人數請用 classroom_student_count_map（轉班歷史感知）。
    """
    query = session.query(func.count(Student.id)).filter(
        student_active_on_filter(target_date)
    )
    if classroom_id is not None:
        query = query.filter(Student.classroom_id == classroom_id)
    return int(query.scalar() or 0)


def classroom_student_count_map(session, target_date: date) -> dict[int, int]:
    """各班在籍人數（轉班歷史感知，2026-06-13 L1b）。

    班級歸屬判定（語意同 gov_moe.monthly_calculator.classroom_at_month_end，
    另補「目標日早於首筆轉班」的 from_classroom 反查）：
      1. 有 transferred_at <= 目標日的轉班 → 最後一筆的 to_classroom_id
      2. 無 1. 但有更晚的轉班 → 最早一筆的 from_classroom_id（轉班前所屬；
         NULL 表示當時尚未分班 → 不計入任何班）
      3. 完全無轉班紀錄 → 現態 classroom_id
    一次撈齊全部相關轉班紀錄，避免逐學生 N+1。
    """
    from models.student_transfer import StudentClassroomTransfer

    rows = (
        session.query(Student.id, Student.classroom_id)
        .filter(student_active_on_filter(target_date))
        .all()
    )
    if not rows:
        return {}

    student_ids = [sid for sid, _ in rows]
    transfers = (
        session.query(
            StudentClassroomTransfer.student_id,
            StudentClassroomTransfer.from_classroom_id,
            StudentClassroomTransfer.to_classroom_id,
            StudentClassroomTransfer.transferred_at,
        )
        .filter(StudentClassroomTransfer.student_id.in_(student_ids))
        .order_by(
            StudentClassroomTransfer.transferred_at.asc(),
            StudentClassroomTransfer.id.asc(),
        )
        .all()
    )

    cutoff = datetime.combine(target_date, time.max)
    last_before: dict[int, int] = {}
    first_after: dict[int, int | None] = {}
    for sid, from_cid, to_cid, at in transfers:
        if at <= cutoff:
            last_before[sid] = to_cid
        elif sid not in first_after:
            first_after[sid] = from_cid

    counts: dict[int, int] = {}
    for sid, current_cid in rows:
        if sid in last_before:
            cid = last_before[sid]
        elif sid in first_after:
            cid = first_after[sid]
        else:
            cid = current_cid
        if cid is None:
            continue
        counts[cid] = counts.get(cid, 0) + 1
    return counts
