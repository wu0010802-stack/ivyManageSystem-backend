"""年終 Excel 再匯入：DRAFT 改 payable 須重算 total_amount；已簽核（SIGNED）不可被覆寫。

bug hunt 2026-06-15 P1#2（services/year_end/excel_io.py import_year_end_to_db update 分支）：
- (a) update 分支設 payable_amount 卻不重算 total_amount；員工不在本次「年終獎金總表」
      特別獎金 sheet 時，total_amount 停在舊值 → 轉帳名冊以錯誤金額匯款（少發/多發）。
- (b) update 分支只擋 FINALIZED，未擋 SUPERVISOR_SIGNED / ACCOUNTING_SIGNED → 已簽核
      （職責分離兩關之一已過）的金額在簽章不變下被靜默改寫。canonical build_settlements
      (settlement_builder.py:898) 與 manual_patch 皆以 != DRAFT 拒絕所有非 DRAFT。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from models.year_end import (
    EmployeeYearEndSnapshot,
    YearEndCycle,
    YearEndSettlement,
    YearEndSettlementStatus,
)
from services.year_end.excel_io import (
    ParsedSettlementRow,
    ParsedYearEndExcel,
    import_year_end_to_db,
)


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


def _make_settlement(session, cycle_id, employee_id, status, payable):
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
        status=status,
    )
    session.add(s)
    session.flush()
    return s


def _parsed_with_payable(name: str, payable: Decimal) -> ParsedYearEndExcel:
    """單一員工、無特別獎金（不在「年終獎金總表」sheet）的解析結果。"""
    return ParsedYearEndExcel(
        academic_year=114,
        settlements=[
            ParsedSettlementRow(
                excel_row=1,
                name=name,
                base_salary=Decimal("30000"),
                festival_total=Decimal("0"),
                avg_performance_rate=Decimal("90"),
                gross_amount=payable,
                org_achievement_rate=Decimal("100"),
                subtotal=payable,
                total_in_year=Decimal("12"),
                payable=payable,
            ),
        ],
        special_bonuses=[],
        class_targets=[],
    )


def _run_import(session, parsed, emp_id):
    import_year_end_to_db(
        parsed,
        session,
        employee_resolver=lambda name: emp_id if name == "王測試" else None,
        cycle_dates=(date(2025, 1, 1), date(2025, 12, 31), date(2026, 1, 15)),
        org_achievement_rate_first=Decimal("100"),
        org_achievement_rate_second=Decimal("100"),
    )
    session.commit()


def test_draft_reimport_changed_payable_recomputes_total(test_db_session):
    """(a) DRAFT settlement 重匯改 payable，員工不在特別獎金 sheet → total_amount 須跟著重算。"""
    session = test_db_session
    cycle = _make_cycle(session)
    EMP_ID = 31
    _make_settlement(
        session, cycle.id, EMP_ID, YearEndSettlementStatus.DRAFT, Decimal("28000")
    )
    session.commit()

    # 第二次匯入：payable 由 28000 改為 35000，無特別獎金
    _run_import(session, _parsed_with_payable("王測試", Decimal("35000")), EMP_ID)

    settlement = (
        session.query(YearEndSettlement)
        .filter_by(year_end_cycle_id=cycle.id, employee_id=EMP_ID)
        .one()
    )
    assert settlement.payable_amount == Decimal("35000")
    assert settlement.total_amount == Decimal("35000"), (
        "total_amount 應隨 payable 重算為 35000（special 0），"
        f"實際 {settlement.total_amount}（stale → 轉帳名冊發錯金額）"
    )


@pytest.mark.parametrize(
    "status",
    [
        YearEndSettlementStatus.SUPERVISOR_SIGNED,
        YearEndSettlementStatus.ACCOUNTING_SIGNED,
    ],
)
def test_signed_settlement_not_overwritten_by_reimport(test_db_session, status):
    """(b) 已簽核（未核定）的 settlement 重匯 Excel 不得被覆寫金額（對齊 canonical != DRAFT）。"""
    session = test_db_session
    cycle = _make_cycle(session)
    EMP_ID = 32
    _make_settlement(session, cycle.id, EMP_ID, status, Decimal("28000"))
    session.commit()

    # 重匯一份金額被改大的 Excel
    _run_import(session, _parsed_with_payable("王測試", Decimal("99999")), EMP_ID)

    settlement = (
        session.query(YearEndSettlement)
        .filter_by(year_end_cycle_id=cycle.id, employee_id=EMP_ID)
        .one()
    )
    assert settlement.payable_amount == Decimal(
        "28000"
    ), "已簽核 settlement 不應被 import 覆寫 payable（簽章不變金額被竄改）"
    assert settlement.base_salary == Decimal("30000")
    assert settlement.total_amount == Decimal("28000")
    assert settlement.status == status
