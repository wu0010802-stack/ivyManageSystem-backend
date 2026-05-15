"""appraisal package — 半年考核計算引擎與 Excel I/O。

入口模組：
- engine：5-step 計算引擎（純函式，無 DB 依賴）
- excel_io：Excel 雙向轉換（114(上)考核統計表 → DB / DB → 列印版）
"""

from .engine import (
    BonusRateLookup,
    HALF_YEAR_MONTHS,
    SummaryComputed,
    classify_grade,
    compute_base_score,
    compute_bonus_amount,
    compute_summary,
    compute_total_score,
    sum_score_items,
)

__all__ = [
    "BonusRateLookup",
    "HALF_YEAR_MONTHS",
    "SummaryComputed",
    "classify_grade",
    "compute_base_score",
    "compute_bonus_amount",
    "compute_summary",
    "compute_total_score",
    "sum_score_items",
]
