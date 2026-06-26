"""固定費用 by-category 月彙總須 round_half_up，不得 int() 朝零截斷（qa-loop P3#8）。

get_monthly_fixed_cost_by_category 對 func.sum(MonthlyFixedCost.amount)（Money=Numeric(12,2)，
水電/餐點/零用金常有角分）用 int() 朝零截斷，每月每類最多少計近 NT$0.99 且恆向下（系統性偏低）。
此值經 build_monthly_pnl 進變動支出與舊制勞退準備金 → /monthly-pnl 支出偏低、淨現金流偏高。
同檔 _month_totals_from（qa-loop #10）已改 round_half_up，此 provider 漏套同一慣例。
"""

from __future__ import annotations

from decimal import Decimal

from models.monthly_fixed_cost import MonthlyFixedCost
from services.finance_report_service import get_monthly_fixed_cost_by_category


def test_fixed_cost_by_category_rounds_half_up_not_truncate(test_db_session):
    s = test_db_session
    s.add_all(
        [
            MonthlyFixedCost(
                year=2026, month=3, category="meals", amount=Decimal("100.99")
            ),
            MonthlyFixedCost(
                year=2026, month=3, category="rent", amount=Decimal("100.50")
            ),
            MonthlyFixedCost(
                year=2026, month=3, category="water", amount=Decimal("100.40")
            ),
        ]
    )
    s.commit()
    out = get_monthly_fixed_cost_by_category(s, 2026)
    assert (
        out[3]["meals"] == 101
    ), f"100.99 應 round_half_up→101（截斷得 100），實得 {out[3].get('meals')}"
    assert (
        out[3]["rent"] == 101
    ), f"100.50 應 round_half_up→101（截斷得 100），實得 {out[3].get('rent')}"
    # .4 以下維持不變（不過度進位）
    assert out[3]["water"] == 100, f"100.40 應維持 100，實得 {out[3].get('water')}"
