"""year_end package — 年終獎金 6-step 計算引擎與 Excel I/O。

入口模組：
- engine：6-step 結算流程（純函式，無 DB 依賴）
"""

from .engine import (
    DeductionBreakdown,
    PerformanceRates,
    SettlementComputed,
    compute_avg_performance_rate,
    compute_deduction_total,
    compute_gross_amount,
    compute_payable_amount,
    compute_proration_rate,
    compute_settlement,
    compute_subtotal_amount,
    compute_total_amount,
)

__all__ = [
    "DeductionBreakdown",
    "PerformanceRates",
    "SettlementComputed",
    "compute_avg_performance_rate",
    "compute_deduction_total",
    "compute_gross_amount",
    "compute_payable_amount",
    "compute_proration_rate",
    "compute_settlement",
    "compute_subtotal_amount",
    "compute_total_amount",
]
