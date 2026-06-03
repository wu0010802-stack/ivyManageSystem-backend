"""年終達成率純查詢：在籍純日期 filter → 全校達成率、班級經營績效。

決策③：在籍判定用純日期條件（enrollment/graduation/withdrawal 三日期），
不依賴 lifecycle_status 現態，可正確計入「歷史月份在籍、事後才退學」的學生，
修正班級經營績效低估問題。withdrawal_date > d 已精確排除「已在 d 當日或之前退學」
的學生，不需再加 lifecycle_status == active 條件。

效能備註：class_performance_rate 逐月以 _classroom_map_at_month_end 批次解析全校
月底班級歸屬（每月固定 2-3 條 query），避免逐生 classroom_at_month_end 的 N+1。
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from models.classroom import Student
from models.student_transfer import StudentClassroomTransfer

_Q2 = Decimal("0.01")


def _q2(x: Decimal | int | str) -> Decimal:
    """四捨五入至小數點後兩位（ROUND_HALF_UP）。"""
    return Decimal(x).quantize(_Q2, rounding=ROUND_HALF_UP)


def _enrolled_on_filter(d: date):
    """純日期在籍 SQL 條件（不依賴 lifecycle 現態，可正確回答歷史月份）。

    條件：
    - enrollment_date IS NULL OR enrollment_date <= d  ← 已入學
    - graduation_date IS NULL OR graduation_date >= d  ← 未畢業
    - withdrawal_date IS NULL OR withdrawal_date > d   ← 該日尚未退學

    不加 lifecycle_status 條件：withdrawal_date > d 已能精確排除「已在 d 當日或之前退學」
    的學生，同時正確計入「後來才退學、但當時仍在籍」的學生（歷史月份正確性）。
    """
    return and_(
        or_(Student.enrollment_date.is_(None), Student.enrollment_date <= d),
        or_(Student.graduation_date.is_(None), Student.graduation_date >= d),
        or_(Student.withdrawal_date.is_(None), Student.withdrawal_date > d),
    )


def count_enrolled_on(
    db: Session,
    d: date,
    classroom_id: int | None = None,
) -> int:
    """指定日期（及可選班級）的嚴格在籍人數。

    注意：指定 classroom_id 時，使用學生目前（現態）的 classroom_id，
    不具轉班歷史感知；若需歷史月份的「當時所在班級」人數，
    請改用 class_performance_rate（內部走 classroom_at_month_end resolver）。
    """
    q = db.query(func.count(Student.id)).filter(_enrolled_on_filter(d))
    if classroom_id is not None:
        q = q.filter(Student.classroom_id == classroom_id)
    return int(q.scalar() or 0)


def school_achievement_rate(
    db: Session,
    basis_date: date,
    target: int | float | Decimal,
) -> Decimal:
    """全校在籍達成率（%）。

    = count_enrolled_on(basis_date) / target × 100，四捨五入至小數點後兩位。
    target <= 0 時回 Decimal("0.00")（防除零）。
    """
    target_d = Decimal(str(target))
    if target_d <= 0:
        return _q2(Decimal("0"))

    actual = count_enrolled_on(db, basis_date)
    return _q2(Decimal(actual) / target_d * Decimal("100"))


def _classroom_map_at_month_end(
    db: Session, student_ids: list[int], snapshot_date: date
) -> dict[int, int | None]:
    """批次版 classroom_at_month_end：一次解析多位學生月底班級歸屬，避免逐生 N+1。

    語意對齊 services.gov_moe.monthly_calculator.classroom_at_month_end：
    取每生 transferred_at <= snapshot_date 當日最後一刻 的最近一筆轉班 to_classroom_id；
    無轉班紀錄者 fallback 現態 Student.classroom_id。
    """
    if not student_ids:
        return {}
    cutoff = datetime.combine(snapshot_date, time.max)
    rows = (
        db.query(
            StudentClassroomTransfer.student_id,
            StudentClassroomTransfer.to_classroom_id,
        )
        .filter(
            StudentClassroomTransfer.student_id.in_(student_ids),
            StudentClassroomTransfer.transferred_at <= cutoff,
        )
        .order_by(
            StudentClassroomTransfer.student_id.asc(),
            StudentClassroomTransfer.transferred_at.desc(),
        )
        .all()
    )
    result: dict[int, int | None] = {}
    for sid, to_cid in rows:
        if sid not in result:  # 已 desc 排序，每生第一筆即最近一筆
            result[sid] = to_cid
    # 無轉班紀錄者 fallback 現態 classroom_id（一次查）
    missing = [sid for sid in student_ids if sid not in result]
    if missing:
        for sid, cid in (
            db.query(Student.id, Student.classroom_id)
            .filter(Student.id.in_(missing))
            .all()
        ):
            result[sid] = cid
    return result


def class_performance_rate(
    db: Session,
    classroom_id: int,
    month_ends: list[date],
    head_count_target: int | float | Decimal,
) -> Decimal:
    """班級經營績效達成率（%）。

    = 各月底該班在班人數平均 / head_count_target × 100，四捨五入至小數點後兩位。

    各月底「在班人數」定義：當日嚴格在籍（純日期：enrollment/graduation/withdrawal），
    且 classroom_at_month_end(student, month_end) == classroom_id。

    head_count_target <= 0 或 month_ends 為空時回 Decimal("0.00")。
    """
    target_d = Decimal(str(head_count_target))
    if target_d <= 0 or not month_ends:
        return _q2(Decimal("0"))

    total_count = 0
    for month_end in month_ends:
        # 取當月底嚴格在籍學生集合（全校）
        candidate_ids = [
            sid
            for (sid,) in db.query(Student.id)
            .filter(_enrolled_on_filter(month_end))
            .all()
        ]
        if not candidate_ids:
            continue
        # 批次解析各生月底班級歸屬（取代逐生 classroom_at_month_end 的 N+1）
        month_map = _classroom_map_at_month_end(db, candidate_ids, month_end)
        total_count += sum(
            1 for sid in candidate_ids if month_map.get(sid) == classroom_id
        )

    avg = Decimal(total_count) / Decimal(len(month_ends))
    return _q2(avg / target_d * Decimal("100"))
