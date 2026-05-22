"""appraisal → year_end 橋接 service。

將學期制考核（AppraisalSummary.bonus_amount）寫入既有 special_bonus_items 表的
APPRAISAL_HALF_BONUS_FIRST/SECOND slot，供 salary engine 2 月 calculate 時 pull。

業務規則：
- payout 發放於 civil_year N 的 2/5
- 含「上學年下學期 (N-1.下)」+「本學年上學期 (N.上)」兩筆
- target year_end_cycles.academic_year = N - 1911 - 1（本學年，民國）
- bonus_type 對 period_label 的 mapping：
    FIRST  = 較早 = N-1.下 → period_label = f"{N-1-1911}下"
    SECOND = 較晚 = N.上   → period_label = f"{N-1911-1}上"
  ⚠️ SpecialBonusType 的 FIRST/SECOND 與 AppraisalCycle.Semester.FIRST/SECOND 反向（前者時間順序、後者學期上下）。
"""

from __future__ import annotations

from models.year_end import SpecialBonusType


def civil_year_to_target_academic_year(civil_year: int) -> int:
    """payout 發放國曆年 N → 對應本學年（民國）。

    2026 國曆年 2/5 = 114 學年下學期初（學年 8 月起算），所以 target = 114。
    """
    return civil_year - 1911 - 1


def map_bonus_type_to_period_label(
    bonus_type: SpecialBonusType, target_academic_year: int
) -> str:
    """FIRST → 前一學年下學期；SECOND → 本學年上學期。"""
    if bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST:
        return f"{target_academic_year - 1}下"
    if bonus_type == SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND:
        return f"{target_academic_year}上"
    raise ValueError(
        f"map_bonus_type_to_period_label 僅支援 APPRAISAL_HALF_BONUS_*；got {bonus_type}"
    )
