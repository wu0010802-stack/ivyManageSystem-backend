"""年終獎金 6 step 計算引擎。

step 1：平均績效 = avg(全校達成率上、下 + 班舊生達成率上、下 + 班經營績效上、下)
        廚工/職員（無班級）→ 跳過班級兩項，僅平均全校兩項
step 2：年終毛額 = (base_salary + festival_total) × 平均績效%
step 3：小計 = 毛額 × org_achievement_rate%
step 4：扣項 = Σ(請假遲到 + 自強活動/機構會議 + 事假 + 病假 + 遲到早退 + 獎懲)
step 5：應領小計 = (小計 - 扣項) × hire_months / 12
step 6：年終總額 = 應領小計 + Σ special_bonus_items
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import RoleGroup
from models.year_end import (
    SettlementStatus,
    YearEndClassTarget,
    YearEndCycle,
    YearEndEmployeeSnapshot,
    YearEndOrgSettings,
    YearEndSettlement,
    YearEndSpecialBonusItem,
)

from .constants import FULL_YEAR_MONTHS

logger = logging.getLogger(__name__)


# ── step 1 ──────────────────────────────────────────────────────────────────


def compute_avg_performance(
    org_settings: YearEndOrgSettings,
    class_target: Optional[YearEndClassTarget],
    role_group: RoleGroup,
) -> tuple[Decimal, dict]:
    """回傳 (avg_pct, breakdown) — avg_pct 為 0~150 範圍的百分比數值。

    breakdown 寫入 settlement.calc_meta 供稽核。
    """
    parts: list[tuple[str, Decimal]] = [
        ("achievement_rate_first", org_settings.achievement_rate_first),
        ("achievement_rate_second", org_settings.achievement_rate_second),
    ]

    has_class = role_group not in (RoleGroup.STAFF, RoleGroup.COOK) and class_target is not None
    if has_class:
        parts.extend(
            [
                ("class_returning_first", class_target.returning_rate_first),
                ("class_returning_second", class_target.returning_rate_second),
                ("class_achievement_first", class_target.achievement_rate_first),
                ("class_achievement_second", class_target.achievement_rate_second),
            ]
        )

    total: Decimal = sum((v for _, v in parts), Decimal("0"))
    avg = (total / Decimal(len(parts))).quantize(Decimal("0.01"))
    return avg, {"parts": [(k, str(v)) for k, v in parts], "count": len(parts)}


# ── step 2 ──────────────────────────────────────────────────────────────────


def compute_gross_amount(
    base_salary: Decimal,
    festival_total: Decimal,
    avg_performance_pct: Decimal,
) -> Decimal:
    pct = avg_performance_pct / Decimal("100")
    return ((base_salary + festival_total) * pct).quantize(Decimal("0.01"))


# ── step 3 ──────────────────────────────────────────────────────────────────


def compute_subtotal(gross: Decimal, org_achievement_pct: Decimal) -> Decimal:
    pct = org_achievement_pct / Decimal("100")
    return (gross * pct).quantize(Decimal("0.01"))


# ── step 5 ──────────────────────────────────────────────────────────────────


def compute_payable_subtotal(
    subtotal: Decimal,
    deduction_total: Decimal,
    hire_months: Decimal,
) -> Decimal:
    base = subtotal - deduction_total
    ratio = (hire_months / FULL_YEAR_MONTHS).quantize(Decimal("0.0001"))
    if ratio > Decimal("1"):
        ratio = Decimal("1")
    if ratio < Decimal("0"):
        ratio = Decimal("0")
    return (base * ratio).quantize(Decimal("0.01"))


# ── step 6 ──────────────────────────────────────────────────────────────────


def compute_total_amount(payable_subtotal: Decimal, special_sum: Decimal) -> Decimal:
    return (payable_subtotal + special_sum).quantize(Decimal("0.01"))


# ── helpers ────────────────────────────────────────────────────────────────


def _sum_special_bonus(db: Session, cycle_id: int, employee_id: int) -> Decimal:
    rows = (
        db.execute(
            select(YearEndSpecialBonusItem.amount).where(
                YearEndSpecialBonusItem.cycle_id == cycle_id,
                YearEndSpecialBonusItem.employee_id == employee_id,
            )
        )
        .scalars()
        .all()
    )
    return sum(rows, Decimal("0")).quantize(Decimal("0.01"))


def _hire_months_in_cycle(snapshot: YearEndEmployeeSnapshot, cycle_year: int) -> Decimal:
    """計算 snapshot 對應員工在該年的到職月數（簡化版：以 hire_date.year 與 cycle_year 比較）。

    精細規則（離職 / 試用未滿）由 caller 在建 snapshot 時寫入 is_resigned / is_contracted。
    """
    if snapshot.hire_date is None:
        return FULL_YEAR_MONTHS
    if snapshot.hire_date.year > cycle_year:
        return Decimal("0")
    if snapshot.hire_date.year < cycle_year:
        months = FULL_YEAR_MONTHS
    else:
        # 同年到職：從 hire_date.month 計到 12 月
        months = Decimal(12 - snapshot.hire_date.month + 1)
    if snapshot.is_resigned and snapshot.resign_date and snapshot.resign_date.year == cycle_year:
        end_month = Decimal(snapshot.resign_date.month)
        start_month = (
            Decimal(snapshot.hire_date.month)
            if snapshot.hire_date.year == cycle_year
            else Decimal("1")
        )
        months = max(Decimal("0"), end_month - start_month + 1)
    return months


# ── 主入口 ──────────────────────────────────────────────────────────────────


def settle_employee(
    db: Session,
    *,
    cycle: YearEndCycle,
    snapshot: YearEndEmployeeSnapshot,
    org_settings: YearEndOrgSettings,
    class_target: Optional[YearEndClassTarget],
    deductions: dict[str, Decimal],
) -> YearEndSettlement:
    """計算單一員工的年終 settlement。

    Args:
        deductions: dict with keys late / personal_leave / sick_leave / meeting /
                    disciplinary / parental_leave。各項皆為 Decimal。
    Returns:
        新建（或更新）的 YearEndSettlement，未 commit。
    """
    if not snapshot.is_contracted:
        # 未簽約 → 跳過計算（不寫 settlement）
        logger.info(
            "[year_end] skip uncontracted employee_id=%d cycle=%d",
            snapshot.employee_id,
            cycle.id,
        )
        raise ValueError("snapshot_not_contracted")

    avg_pct, avg_breakdown = compute_avg_performance(
        org_settings, class_target, snapshot.role_group
    )
    gross = compute_gross_amount(
        snapshot.base_salary, snapshot.festival_total, avg_pct
    )
    subtotal = compute_subtotal(gross, org_settings.org_achievement_rate)

    deduction_total = sum(deductions.values(), Decimal("0")).quantize(Decimal("0.01"))
    hire_months = _hire_months_in_cycle(snapshot, cycle.academic_year + 1911)
    payable = compute_payable_subtotal(subtotal, deduction_total, hire_months)
    special_sum = _sum_special_bonus(db, cycle.id, snapshot.employee_id)
    total = compute_total_amount(payable, special_sum)

    existing = db.execute(
        select(YearEndSettlement).where(
            YearEndSettlement.snapshot_id == snapshot.id
        )
    ).scalar_one_or_none()

    if existing is None:
        settlement = YearEndSettlement(
            cycle_id=cycle.id,
            snapshot_id=snapshot.id,
            employee_id=snapshot.employee_id,
        )
        db.add(settlement)
    else:
        settlement = existing
        if settlement.status == SettlementStatus.FINALIZED:
            raise PermissionError(
                "settlement_finalized:cannot_recalculate"
            )

    settlement.avg_performance_rate = avg_pct
    settlement.gross_amount = gross
    settlement.subtotal_amount = subtotal
    settlement.deduction_total = deduction_total
    settlement.deduction_late = deductions.get("late", Decimal("0"))
    settlement.deduction_personal_leave = deductions.get("personal_leave", Decimal("0"))
    settlement.deduction_sick_leave = deductions.get("sick_leave", Decimal("0"))
    settlement.deduction_meeting = deductions.get("meeting", Decimal("0"))
    settlement.deduction_disciplinary = deductions.get("disciplinary", Decimal("0"))
    settlement.deduction_parental_leave = deductions.get(
        "parental_leave", Decimal("0")
    )
    settlement.payable_subtotal = payable
    settlement.special_bonus_sum = special_sum
    settlement.total_amount = total
    settlement.calc_meta = {
        "avg_performance": avg_breakdown,
        "hire_months": str(hire_months),
        "deductions": {k: str(v) for k, v in deductions.items()},
    }
    settlement.status = SettlementStatus.CALCULATED
    return settlement
