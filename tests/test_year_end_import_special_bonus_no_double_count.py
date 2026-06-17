"""年終 Excel 再匯入：同一筆獎金不得因 auto_derive 與 excel 用不同 period_label 而重複計入。

P1（2026-06-17 qa-loop 全掃）：
- auto_derive/festival_diff.period_label 回傳 f"{year}-FD"（如 "114-FD"），
  但 excel_io 匯入 FESTIVAL_DIFF 用 "114.8-115.01"。
- uq 鍵 = (cycle, emp, bonus_type, period_label) 含 period_label → 兩列並存。
- import_year_end_to_db 重算 special_bonus_total 以 SUM(全部列) → 同一筆節慶差額被計入
  兩次 → 年終轉帳名冊對該員工多發一份。
業主裁示：excel 為最終真相，excel 提供的 bonus_type 應覆蓋對應的 auto-derived 列（不可兩列都加）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from models.year_end import (
    EmployeeYearEndSnapshot,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
    YearEndSettlement,
    YearEndSettlementStatus,
)
from services.year_end.excel_io import (
    ParsedSettlementRow,
    ParsedSpecialBonus,
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


def _make_draft_settlement(session, cycle_id, employee_id, payable):
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


def test_excel_festival_diff_overrides_auto_not_double_counted(test_db_session):
    """auto_derive 與 excel 對同一筆 FESTIVAL_DIFF 用不同 label，import 後不得雙計。"""
    session = test_db_session
    cycle = _make_cycle(session)
    EMP_ID = 31
    _make_draft_settlement(session, cycle.id, EMP_ID, Decimal("30000"))

    # auto_derive 先寫入 FESTIVAL_DIFF（label "114-FD"）
    session.add(
        SpecialBonusItem(
            year_end_cycle_id=cycle.id,
            employee_id=EMP_ID,
            bonus_type=SpecialBonusType.FESTIVAL_DIFF,
            period_label=f"{cycle.academic_year}-FD",
            amount=Decimal("1000"),
            classroom_id=None,
            calc_meta={},
            source_ref="auto:festival_diff",
        )
    )
    session.commit()

    # HR 匯入年終總表：同一員工 FESTIVAL_DIFF=1000（excel label "114.8-115.01"）
    parsed = ParsedYearEndExcel(
        academic_year=114,
        settlements=[
            ParsedSettlementRow(
                excel_row=1,
                name="王測試",
                base_salary=Decimal("30000"),
                festival_total=Decimal("0"),
                avg_performance_rate=Decimal("90"),
                gross_amount=Decimal("30000"),
                org_achievement_rate=Decimal("100"),
                subtotal=Decimal("30000"),
                total_in_year=Decimal("12"),
                payable=Decimal("30000"),
            ),
        ],
        special_bonuses=[
            ParsedSpecialBonus(
                name="王測試",
                bonus_type=SpecialBonusType.FESTIVAL_DIFF,
                period_label="114.8-115.01",
                amount=Decimal("1000"),
                calc_meta={},
            ),
        ],
        class_targets=[],
    )
    import_year_end_to_db(
        parsed,
        session,
        employee_resolver=lambda name: EMP_ID if name == "王測試" else None,
        cycle_dates=(date(2025, 1, 1), date(2025, 12, 31), date(2026, 1, 15)),
        org_achievement_rate_first=Decimal("100"),
        org_achievement_rate_second=Decimal("100"),
    )
    session.commit()

    settlement = (
        session.query(YearEndSettlement)
        .filter_by(year_end_cycle_id=cycle.id, employee_id=EMP_ID)
        .one()
    )
    assert settlement.special_bonus_total == Decimal("1000"), (
        "同一筆節慶差額不得重複計入（excel 為最終真相，應覆蓋 auto 列），"
        f"實際 {settlement.special_bonus_total}（2000=auto+excel 雙計 → 轉帳多發）"
    )
    assert settlement.total_amount == Decimal(
        "31000"
    ), f"total_amount 應為 payable 30000 + special 1000 = 31000，實際 {settlement.total_amount}"
