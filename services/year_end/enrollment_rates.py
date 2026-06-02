"""年終達成率純查詢：在籍嚴格 filter（排除已退學）→ 全校達成率、班級經營績效。

決策③：在籍判定用嚴格條件（排除已退學 / lifecycle 非 active），統一於本檔，
不直接用 services/student_enrollment.count_students_active_on（它只看
enrollment/graduation 日期、不排退學）。

效能備註：class_performance_rate 逐月呼叫 classroom_at_month_end resolver，
階段 1 O(學生×月) 可接受；正確性優先。
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from models.classroom import LIFECYCLE_ACTIVE, Student
from services.gov_moe.monthly_calculator import classroom_at_month_end

_Q2 = Decimal("0.01")


def _q2(x: Decimal | int | str) -> Decimal:
    """四捨五入至小數點後兩位（ROUND_HALF_UP）。"""
    return Decimal(x).quantize(_Q2, rounding=ROUND_HALF_UP)


def _strict_enrolled_filter(d: date):
    """嚴格在籍 SQL 條件（排除退學/非 active lifecycle）。

    條件：
    - enrollment_date IS NULL OR enrollment_date <= d
    - graduation_date IS NULL OR graduation_date >= d
    - withdrawal_date IS NULL OR withdrawal_date > d   ← 比 student_enrollment 更嚴格
    - lifecycle_status == 'active'                      ← 排退學、已轉出等終態
    """
    return and_(
        or_(Student.enrollment_date.is_(None), Student.enrollment_date <= d),
        or_(Student.graduation_date.is_(None), Student.graduation_date >= d),
        or_(Student.withdrawal_date.is_(None), Student.withdrawal_date > d),
        Student.lifecycle_status == LIFECYCLE_ACTIVE,
    )


def count_enrolled_on(
    db: Session,
    d: date,
    classroom_id: int | None = None,
) -> int:
    """指定日期（及可選班級）的嚴格在籍人數。"""
    q = db.query(func.count(Student.id)).filter(_strict_enrolled_filter(d))
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

    各月底「在班人數」定義：當日嚴格在籍（lifecycle == active、未退學），
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
            db.query(Student.id).filter(_strict_enrolled_filter(month_end)).all()
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
