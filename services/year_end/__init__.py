"""年終獎金 service package。

入口：
- `engine`：6 step 計算（平均績效 → 毛額 → 小計 → 扣項 → 應領小計 → 總額）
- `snapshot`：建立 employee snapshot（含 festival_total 串接 services.salary.festival）
- `deductions`：扣項聚合（attendance / leave / disciplinary / score_items）
- `constants`：節慶月（2/6/9/12）等
"""

from .engine import (
    compute_avg_performance,
    compute_gross_amount,
    compute_subtotal,
    compute_payable_subtotal,
    compute_total_amount,
    settle_employee,
)

__all__ = [
    "compute_avg_performance",
    "compute_gross_amount",
    "compute_subtotal",
    "compute_payable_subtotal",
    "compute_total_amount",
    "settle_employee",
]
