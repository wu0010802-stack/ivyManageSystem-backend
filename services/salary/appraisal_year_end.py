"""Salary engine plugin：2 月 calculate 時拉考核年終獎金。

source of truth = special_bonus_items（FIRST+SECOND 兩筆）；每月 calculate 重新 query。
2 月份以外 return 0；不進 gross_salary、不影響勞健保 / 應發合計。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.year_end import SpecialBonusItem, SpecialBonusType, YearEndCycle
from services.year_end.appraisal_sync import civil_year_to_target_academic_year


def query_appraisal_year_end_bonus(
    db: Session, employee_id: int, year: int, month: int
) -> Decimal:
    """2 月份 query special_bonus_items 兩筆 APPRAISAL_HALF_BONUS_* 的 SUM。

    其他月份 return Decimal(0)。
    target_academic_year = year - 1911 - 1 (e.g., 2026 → 114)。
    """
    if month != 2:
        return Decimal(0)
    target_academic_year = civil_year_to_target_academic_year(year)
    result = db.scalar(
        select(func.coalesce(func.sum(SpecialBonusItem.amount), 0))
        .join(YearEndCycle, YearEndCycle.id == SpecialBonusItem.year_end_cycle_id)
        .where(
            YearEndCycle.academic_year == target_academic_year,
            SpecialBonusItem.employee_id == employee_id,
            SpecialBonusItem.bonus_type.in_(
                [
                    SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                    SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
                ]
            ),
        )
    )
    return Decimal(result or 0)
