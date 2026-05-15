"""半年考核 service package。

入口模組：
- `engine`：5 step 計算（base_score → item_sum → total_score → grade → bonus）
- `constants`：等第切點、grade × role base、grade pct map
- `catalog`：score_item catalog 取用、validator
- `summary_ops`：recompute_summary / mark_stale 副作用
- `excel_io`：Excel import / export
- `cycle_dates`：學期日期工具（沿用舊版邏輯）
"""

from .engine import (
    classify_grade,
    compute_bonus_amount,
    compute_total_score,
    recompute_summary,
)
from .constants import (
    GRADE_BONUS_PCT,
    MAX_TOTAL_SCORE,
    MIN_TOTAL_SCORE,
)
from .summary_ops import (
    mark_summary_stale,
    load_active_rates_map,
)
from .cycle_dates import default_calc_date, default_cycle_dates, suggest_role_group

__all__ = [
    "classify_grade",
    "compute_bonus_amount",
    "compute_total_score",
    "recompute_summary",
    "GRADE_BONUS_PCT",
    "MAX_TOTAL_SCORE",
    "MIN_TOTAL_SCORE",
    "mark_summary_stale",
    "load_active_rates_map",
    "default_calc_date",
    "default_cycle_dates",
    "suggest_role_group",
]
