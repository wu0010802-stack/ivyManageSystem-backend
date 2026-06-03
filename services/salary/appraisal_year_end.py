"""Salary engine plugin：2 月 calculate 時拉考核年終獎金。

DEPRECATED (決策⑥B 2026-06-02): salary engine 不再呼叫此模組（engine.py 已移除
import，runtime 零 caller）；考核年終已併入 year_end_settlements 獨立轉帳。
函式**刻意保留**：① 仍正確且有 standalone 測試（test_salary_appraisal_year_end_plugin
/ test_salary_bulk_preload_helpers）② 同檔 engine guard 測試
（test_salary_engine_does_not_call_appraisal_year_end_plugin /
test_engine_does_not_pull_appraisal_in_february）以「函式在、但 engine 不呼叫」強制
決策⑥B。刪此模組須一併移除上述 guard → 失去保護網，預設不刪。

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

    DEPRECATED (決策⑥B 2026-06-02): salary engine 不再呼叫；考核年終已併入
    year_end_settlements 獨立轉帳。函式刻意保留（理由見模組 docstring：standalone + engine guard 測試）。

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


def query_appraisal_year_end_bonus_bulk(
    db: Session, employee_ids: list[int], year: int, month: int
) -> dict[int, Decimal]:
    """批次版 query_appraisal_year_end_bonus：回 {employee_id: Decimal}。

    DEPRECATED (決策⑥B 2026-06-02): salary engine 不再呼叫；考核年終已併入
    year_end_settlements 獨立轉帳。函式刻意保留（理由見模組 docstring：standalone + engine guard 測試）。

    語意與 per-employee 版一致：非 2 月或無資料者回 Decimal(0)。回傳 Decimal（直接
    寫進 SalaryRecord.appraisal_year_end_bonus 並進下月累計，型別不可降為 float）。
    """
    result = {eid: Decimal(0) for eid in employee_ids}
    if month != 2 or not employee_ids:
        return result
    target_academic_year = civil_year_to_target_academic_year(year)
    rows = db.execute(
        select(
            SpecialBonusItem.employee_id,
            func.coalesce(func.sum(SpecialBonusItem.amount), 0),
        )
        .join(YearEndCycle, YearEndCycle.id == SpecialBonusItem.year_end_cycle_id)
        .where(
            YearEndCycle.academic_year == target_academic_year,
            SpecialBonusItem.employee_id.in_(employee_ids),
            SpecialBonusItem.bonus_type.in_(
                [
                    SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                    SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
                ]
            ),
        )
        .group_by(SpecialBonusItem.employee_id)
    ).all()
    for eid, total in rows:
        result[eid] = Decimal(total or 0)
    return result
