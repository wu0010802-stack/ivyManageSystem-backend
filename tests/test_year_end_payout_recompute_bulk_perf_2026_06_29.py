"""T2（效能，2026-06-29 健檢 A2）：年終 payout 重算對多名員工只掃一次全 cycle。

`appraisal_sync.generate_payouts` / `void_payouts` 原本對每位受影響員工各呼一次
`_recompute_draft_settlement_total`，後者內部 `compute_special_bonus_total_by_emp`
掃**整個 cycle 全員**再 group → O(受影響員工 × 全 SpecialBonusItem 全表)。

本測試固化「批次重算只掃一次」：`_recompute_draft_settlement_totals_bulk` 對 N 名員工
只呼 `compute_special_bonus_total_by_emp` 一次，且每位員工的 total 結果與逐筆重算一致。
excel-wins 去重口徑由既有 test_year_end_recompute_excel_wins 固化，本檔只測掃描次數 + 多人正確性。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select

import services.year_end.settlement_builder as sb
from models.year_end import (
    EmployeeYearEndSnapshot,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
    YearEndSettlement,
    YearEndSettlementStatus,
)
from services.year_end.appraisal_sync import _recompute_draft_settlement_totals_bulk


def _make_cycle(session, academic_year: int = 114) -> YearEndCycle:
    cycle = YearEndCycle(
        academic_year=academic_year,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        bonus_calc_date=date(2026, 1, 15),
    )
    session.add(cycle)
    session.flush()
    return cycle


def _make_draft_settlement(
    session, cycle_id, employee_id, payable
) -> YearEndSettlement:
    snap = EmployeeYearEndSnapshot(
        year_end_cycle_id=cycle_id,
        employee_id=employee_id,
        base_salary=Decimal("30000"),
        festival_total=Decimal("0"),
        hire_months=Decimal("12"),
        is_contracted=True,
        extra={},
    )
    session.add(snap)
    session.flush()
    s = YearEndSettlement(
        year_end_cycle_id=cycle_id,
        employee_id=employee_id,
        snapshot_id=snap.id,
        avg_performance_rate=Decimal("90"),
        base_salary=Decimal("30000"),
        festival_total=Decimal("0"),
        gross_amount=Decimal("30000"),
        org_achievement_rate=Decimal("100"),
        subtotal_amount=Decimal("30000"),
        deduction_leave_late=Decimal("0"),
        deduction_meeting=Decimal("0"),
        deduction_personal_leave=Decimal("0"),
        deduction_sick_leave=Decimal("0"),
        deduction_late=Decimal("0"),
        deduction_disciplinary=Decimal("0"),
        deduction_total=Decimal("0"),
        hire_months=Decimal("12"),
        proration_rate=Decimal("1.0000"),
        payable_amount=payable,
        total_amount=payable,
        special_bonus_total=Decimal("0"),
        status=YearEndSettlementStatus.DRAFT,
    )
    session.add(s)
    session.flush()
    return s


def _item(session, cycle_id, emp_id, btype, label, amount, source_ref):
    session.add(
        SpecialBonusItem(
            year_end_cycle_id=cycle_id,
            employee_id=emp_id,
            bonus_type=btype,
            period_label=label,
            amount=Decimal(str(amount)),
            classroom_id=None,
            calc_meta={},
            source_ref=source_ref,
        )
    )


def test_bulk_recompute_scans_special_bonus_once_for_many_employees(test_db_session):
    """批次重算多名員工：compute_special_bonus_total_by_emp 只掃一次（非 per-employee）。"""
    s = test_db_session
    cycle = _make_cycle(s)
    emp_ids = [51, 52, 53]
    for emp in emp_ids:
        _make_draft_settlement(s, cycle.id, emp, Decimal("30000"))
        _item(
            s,
            cycle.id,
            emp,
            SpecialBonusType.FESTIVAL_DIFF,
            "114-FD",
            1000,
            "auto:festival_diff",
        )
    s.flush()

    real = sb.compute_special_bonus_total_by_emp
    with patch.object(
        sb, "compute_special_bonus_total_by_emp", side_effect=real
    ) as spy:
        _recompute_draft_settlement_totals_bulk(s, cycle.id, emp_ids)

    assert spy.call_count == 1, (
        f"批次重算 {len(emp_ids)} 名員工應只掃一次全 cycle special bonus，"
        f"實際呼叫 {spy.call_count} 次（= O(員工×全表)，未批次化）"
    )

    for emp in emp_ids:
        settlement = s.scalar(
            select(YearEndSettlement).where(
                YearEndSettlement.year_end_cycle_id == cycle.id,
                YearEndSettlement.employee_id == emp,
            )
        )
        assert settlement.special_bonus_total == Decimal("1000"), (
            f"emp {emp} special_bonus_total 應為 1000，實際 "
            f"{settlement.special_bonus_total}"
        )
        assert settlement.total_amount == Decimal(
            "31000"
        ), f"emp {emp} total_amount 應為 31000，實際 {settlement.total_amount}"


def test_bulk_recompute_empty_employee_ids_is_noop(test_db_session):
    """空名單不掃描（避免無謂的全表掃）。"""
    s = test_db_session
    cycle = _make_cycle(s)

    with patch.object(sb, "compute_special_bonus_total_by_emp") as spy:
        _recompute_draft_settlement_totals_bulk(s, cycle.id, [])

    assert spy.call_count == 0, "空員工名單應 no-op，不得掃描 special bonus"
