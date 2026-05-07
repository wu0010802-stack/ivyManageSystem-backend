"""5 表合成 → IvyKids 級距（對齊既有 INSURANCE_TABLE_2026 規則）。

合成邏輯（T0 fixture 反推，詳見 tests/fixtures/gov_data/_COMPOSER_JOIN_RULES.md）：
- amount = labor_brackets ∪ pension ∪ nhi_brackets，去重排序
- labor_employee/employer：查 labor_premium.by_amount[X]，X = clamp(amount, 11100, 45800)
- pension = round(min(amount, 150000) × 0.06)
- health_employee/employer：查 nhi_premium.by_amount[Y]，Y 為對應健保級距
  - amount <= NHI 最低 → Y = NHI 最低
  - amount >= NHI 最高 → Y = NHI 最高
  - 其他 → Y = nhi_amounts 中 ≥ amount 的最小級
- health_employer 直接取 single.employer，**不加權**眷屬

若某 X/Y 在 premium 表中缺，視為資料不一致 → raise ComposeError。
"""

from __future__ import annotations

import logging

from services.gov_data.schemas import (
    BracketRow,
    ComposedBrackets,
    LaborBracketsResult,
    LaborPremiumResult,
    MinimumWageResult,
    NhiBracketsResult,
    NhiPremiumResult,
    PensionResult,
)

logger = logging.getLogger(__name__)

# 勞保上下限為 IvyKids 級距常用情境，hardcode 即可（每年公告若改動，更新此處 + fixture）
LABOR_MIN = 11100
LABOR_MAX = 45800
PENSION_MAX = 150000


class ComposeError(ValueError):
    """5 表資料不一致無法合成。"""


def _clamp_labor(amount: int) -> int:
    return min(max(amount, LABOR_MIN), LABOR_MAX)


def _resolve_nhi_x(amount: int, nhi_amounts: list[int]) -> int:
    """取 NHI 對應級距：amount < min → min；> max → max；其他 → ≥ amount 的最小級。"""
    if not nhi_amounts:
        raise ComposeError("nhi_premium amounts 為空")
    sorted_amounts = sorted(nhi_amounts)
    nhi_min = sorted_amounts[0]
    nhi_max = sorted_amounts[-1]
    if amount <= nhi_min:
        return nhi_min
    if amount >= nhi_max:
        return nhi_max
    candidates = [a for a in sorted_amounts if a >= amount]
    return min(candidates)


def compose_brackets(
    effective_year: int,
    labor_brackets: LaborBracketsResult,
    labor_premium: LaborPremiumResult,
    pension: PensionResult,
    nhi_brackets: NhiBracketsResult,
    nhi_premium: NhiPremiumResult,
    composed_from: dict[str, int],
) -> ComposedBrackets:
    amounts = sorted(
        set(labor_brackets.amounts) | set(pension.amounts) | set(nhi_brackets.amounts)
    )
    if not amounts:
        raise ComposeError("amount 聯集為空")

    nhi_keys = list(nhi_premium.by_amount.keys())
    rows: list[BracketRow] = []

    for amt in amounts:
        labor_x = _clamp_labor(amt)
        nhi_y = _resolve_nhi_x(amt, nhi_keys)
        try:
            lp = labor_premium.by_amount[labor_x]
        except KeyError as exc:
            raise ComposeError(
                f"compose {effective_year}: amount={amt} → labor_x={labor_x} "
                f"在 labor_premium 表中缺資料: {exc}"
            )
        try:
            np = nhi_premium.by_amount[nhi_y]
            nhi_single = np["single"]
        except (KeyError, TypeError) as exc:
            raise ComposeError(
                f"compose {effective_year}: amount={amt} → nhi_y={nhi_y} "
                f"在 nhi_premium 表中缺資料或 single 結構不對: {exc}"
            )

        rows.append(
            BracketRow(
                amount=amt,
                labor_employee=lp["labor_employee"],
                labor_employer=lp["labor_employer"],
                health_employee=nhi_single["employee"],
                health_employer=nhi_single["employer"],
                pension=round(min(amt, PENSION_MAX) * 0.06),
            )
        )

    rates = {
        "labor_max_insured": labor_brackets.max_insured,
        "health_max_insured": nhi_brackets.max_insured,
        "pension_max_insured": pension.max_insured,
    }
    return ComposedBrackets(
        effective_year=effective_year,
        rows=rows,
        rates=rates,
        composed_from=composed_from,
    )


def compose_minimum_wage(result: MinimumWageResult) -> tuple:
    """基本工資合成：取最新一筆。回傳 (effective_date, monthly, hourly)。"""
    latest = result.latest()
    if latest is None:
        raise ComposeError("基本工資 result 為空")
    return latest
