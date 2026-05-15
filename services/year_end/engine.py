"""年終獎金 6-step 計算引擎（M3）— 純函式設計，無 DB 依賴。

對應 Excel「114年年終經營績效」「年終獎金」「年終獎金總表」三 sheet 的串聯計算：

  step1 avg_performance_rate = avg(全校達成率上, 下, 班舊生達成率上, 下,
                                   班經營績效上, 下)
  step2 gross_amount = (base_salary + festival_total) × avg_performance_rate%
  step3 subtotal_amount = gross_amount × org_achievement_rate%（83.6/91.5）
  step4 deduction_total = Σ(請假遲到 + 自強活動/機構會議 + 事假 + 病假 + 遲到早退 + 奬懲)
  step5 payable_amount = (subtotal + deduction_total) × hire_months/12
  step6 total_amount = payable + Σ special_bonus_items.amount

驗證 case（Excel「年終獎金」蔡宜倩）：
  base=36160, festival=2000, avg=97.0%, org_rate=83.6%
  gross  = 38160 × 0.97       = 37015.20
  subtot = 37015.20 × 0.836   = 30944.71
  deduct = -1000 + -900       = -1900
  合計    = 30944.71 + -1900   = 29044.71
  payable = 29044.71 × 1.0     = 29044.71
  special = 3312 + 1500 + 1000 + 1275 + 2000 + 1975 = 11062
  total   = 29044.71 + 11062   = 40106.71 ≈ Excel 40106.7072

設計原則：
- Decimal 全程；step2/3/5 用 quantize(0.01, HALF_UP) 保留 2 位小數
- 角色無班級績效（STAFF/COOK）→ avg 僅以「全校達成率」計算（避開 None）
- 未簽約 / 不計入年終 → caller 不應呼叫引擎
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

logger = logging.getLogger(__name__)

# 年度週期最大月數（plan §5 邊界 case：到職未滿一年比例底數）
YEAR_MONTHS = Decimal("12")

# Numeric helpers
_TWO_PLACES = Decimal("0.01")
_FOUR_PLACES = Decimal("0.0001")


def _q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def _q4(x: Decimal) -> Decimal:
    return Decimal(x).quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Step 1: 平均績效
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PerformanceRates:
    """step1 的 6 個（最多）輸入率，皆為百分比（如 75.6 代表 75.6%）。

    STAFF / COOK 等無班級績效 → class_* 兩對欄位皆為 None。
    上下學期任一缺值（如新員工只算到一學期）→ 該欄位為 None，
    平均時自動跳過。
    """

    school_rate_first: Optional[Decimal] = None
    school_rate_second: Optional[Decimal] = None
    class_returning_rate_first: Optional[Decimal] = None
    class_returning_rate_second: Optional[Decimal] = None
    class_performance_rate_first: Optional[Decimal] = None
    class_performance_rate_second: Optional[Decimal] = None

    def all_rates(self) -> list[Decimal]:
        return [
            r
            for r in (
                self.school_rate_first,
                self.school_rate_second,
                self.class_returning_rate_first,
                self.class_returning_rate_second,
                self.class_performance_rate_first,
                self.class_performance_rate_second,
            )
            if r is not None
        ]


def compute_avg_performance_rate(rates: PerformanceRates) -> Decimal:
    """step1: 平均績效百分比（兩位小數）。

    Excel 邏輯：上下兩學期先平均成「全校達成率平均」「班舊生平均」「班經營平均」三項，
    再三項取算術平均。Excel 範例：
      (75.6 + 91.5)/2 = 83.55
      (92.6 + 94.9)/2 = 93.75
      (94.4 + 88.5)/2 = 91.45
      (83.55 + 93.75 + 91.45)/3 = 89.5833 ≈ 89.58

    若某對缺一邊（如員工新到職下學期），用單邊；若整對皆 None，視為無此項。
    """

    def pair_avg(a: Optional[Decimal], b: Optional[Decimal]) -> Optional[Decimal]:
        vals = [v for v in (a, b) if v is not None]
        if not vals:
            return None
        total = sum(vals, Decimal("0"))
        return total / Decimal(len(vals))

    school = pair_avg(rates.school_rate_first, rates.school_rate_second)
    cls_returning = pair_avg(
        rates.class_returning_rate_first, rates.class_returning_rate_second
    )
    cls_perf = pair_avg(
        rates.class_performance_rate_first, rates.class_performance_rate_second
    )
    components = [c for c in (school, cls_returning, cls_perf) if c is not None]
    if not components:
        return Decimal("0.0")
    avg = sum(components, Decimal("0")) / Decimal(len(components))
    # Excel「年終獎金」sheet 使用 1 位小數（如 96.95 → 97.0），下游公式才能對齊。
    return Decimal(avg).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Step 2: 毛額
# ---------------------------------------------------------------------------


def compute_gross_amount(
    base_salary: Decimal, festival_total: Decimal, avg_performance_rate: Decimal
) -> Decimal:
    """step2: gross = (base_salary + festival_total) × avg_performance_rate%。"""
    sum_pay = Decimal(base_salary) + Decimal(festival_total)
    raw = sum_pay * Decimal(avg_performance_rate) / Decimal("100")
    return _q2(raw)


# ---------------------------------------------------------------------------
# Step 3: 小計
# ---------------------------------------------------------------------------


def compute_subtotal_amount(
    gross_amount: Decimal, org_achievement_rate: Decimal
) -> Decimal:
    """step3: subtotal = gross × org_achievement_rate%（如 83.6 / 91.5）。"""
    raw = Decimal(gross_amount) * Decimal(org_achievement_rate) / Decimal("100")
    return _q2(raw)


# ---------------------------------------------------------------------------
# Step 4: 扣項
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeductionBreakdown:
    """6 項扣款明細；皆為負或 0（caller 傳入符號）。

    對應 Excel「年終獎金」sheet 的 6 個扣款欄：
      請假遲到（去年 02-12 月合併）
      奬懲（如大過 -6000）
      自強活動/機構會議（每次 -1000）
      事假
      病假/育嬰假
      遲到/早退
    """

    leave_late_prev: Decimal = field(default_factory=lambda: Decimal("0"))
    disciplinary: Decimal = field(default_factory=lambda: Decimal("0"))
    meeting: Decimal = field(default_factory=lambda: Decimal("0"))
    personal_leave: Decimal = field(default_factory=lambda: Decimal("0"))
    sick_leave: Decimal = field(default_factory=lambda: Decimal("0"))
    late_early: Decimal = field(default_factory=lambda: Decimal("0"))


def compute_deduction_total(d: DeductionBreakdown) -> Decimal:
    """step4: deduction_total = Σ 各項；結果為負或 0。"""
    total = (
        Decimal(d.leave_late_prev)
        + Decimal(d.disciplinary)
        + Decimal(d.meeting)
        + Decimal(d.personal_leave)
        + Decimal(d.sick_leave)
        + Decimal(d.late_early)
    )
    return _q2(total)


# ---------------------------------------------------------------------------
# Step 5: 應領小計
# ---------------------------------------------------------------------------


def compute_proration_rate(hire_months: Decimal) -> Decimal:
    """到職比例 = hire_months / 12，clamp 0-1（保留 4 位小數）。"""
    rate = Decimal(hire_months) / YEAR_MONTHS
    if rate < Decimal("0"):
        return Decimal("0.0000")
    if rate > Decimal("1"):
        return Decimal("1.0000")
    return _q4(rate)


def compute_payable_amount(
    subtotal_amount: Decimal,
    deduction_total: Decimal,
    proration_rate: Decimal,
) -> Decimal:
    """step5: payable = (subtotal + deduction_total) × proration_rate。

    Excel 範例：郭玟秀 17558.826 × 10/12 = 14632.355
    """
    after_deduction = Decimal(subtotal_amount) + Decimal(deduction_total)
    raw = after_deduction * Decimal(proration_rate)
    return _q2(raw)


# ---------------------------------------------------------------------------
# Step 6: 年終總額
# ---------------------------------------------------------------------------


def compute_total_amount(
    payable_amount: Decimal, special_bonus_total: Decimal
) -> Decimal:
    """step6: total = payable + Σ special_bonus_items.amount。"""
    return _q2(Decimal(payable_amount) + Decimal(special_bonus_total))


# ---------------------------------------------------------------------------
# 高階：整合 6 step
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettlementComputed:
    """6-step 結算結果；caller 用於 upsert year_end_settlements。"""

    avg_performance_rate: Decimal
    gross_amount: Decimal
    subtotal_amount: Decimal
    deduction_total: Decimal
    payable_amount: Decimal
    special_bonus_total: Decimal
    total_amount: Decimal
    proration_rate: Decimal


def compute_settlement(
    *,
    base_salary: Decimal,
    festival_total: Decimal,
    performance_rates: PerformanceRates,
    org_achievement_rate: Decimal,
    deductions: DeductionBreakdown,
    hire_months: Decimal,
    special_bonus_total: Decimal,
) -> SettlementComputed:
    """便利函式：一次跑完 6 step。"""
    avg_rate = compute_avg_performance_rate(performance_rates)
    gross = compute_gross_amount(base_salary, festival_total, avg_rate)
    subtotal = compute_subtotal_amount(gross, org_achievement_rate)
    deduction_total = compute_deduction_total(deductions)
    prate = compute_proration_rate(hire_months)
    payable = compute_payable_amount(subtotal, deduction_total, prate)
    total = compute_total_amount(payable, special_bonus_total)
    return SettlementComputed(
        avg_performance_rate=avg_rate,
        gross_amount=gross,
        subtotal_amount=subtotal,
        deduction_total=deduction_total,
        payable_amount=payable,
        special_bonus_total=Decimal(special_bonus_total),
        total_amount=total,
        proration_rate=prate,
    )
