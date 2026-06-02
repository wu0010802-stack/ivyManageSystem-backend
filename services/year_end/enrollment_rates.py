"""年終達成率純查詢：在籍純日期 filter → 全校達成率、班級經營績效。

決策③：在籍判定用純日期條件（enrollment/graduation/withdrawal 三日期），
不依賴 lifecycle_status 現態，可正確計入「歷史月份在籍、事後才退學」的學生，
修正班級經營績效低估問題。withdrawal_date > d 已精確排除「已在 d 當日或之前退學」
的學生，不需再加 lifecycle_status == active 條件。

效能備註：class_performance_rate 逐月呼叫 classroom_at_month_end resolver，
階段 1 O(學生×月) 可接受；正確性優先。
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from models.classroom import Student
from services.gov_moe.monthly_calculator import classroom_at_month_end

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
    """指定日期（及可選班級）的嚴格在籍人數。"""
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
        candidates = (
            db.query(Student.id).filter(_enrolled_on_filter(month_end)).all()
        )
        # 逐生確認月底班級歸屬
        month_count = sum(
            1
            for (student_id,) in candidates
            if classroom_at_month_end(db, student_id, month_end) == classroom_id
        )
        total_count += month_count

    avg = Decimal(total_count) / Decimal(len(month_ends))
    return _q2(avg / target_d * Decimal("100"))
