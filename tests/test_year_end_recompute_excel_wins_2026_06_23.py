"""年終 special_bonus_total 兩條 recompute 路徑也須套 excel-wins 去重。

qa-loop finding #1（2026-06-23）：canonical `build_settlements` 以
`compute_special_bonus_total_by_emp` 對每個 (emp, bonus_type) 套「excel 最終真相」去重
（有 source_ref=='年終獎金總表' 的 Excel 列即排除同型 source_ref 'auto:' 開頭的 auto 列）。
但下列兩條 recompute 路徑改用裸 SUM(全部 SpecialBonusItem.amount)，未去重：
  - `_recompute_draft_settlement_total`（appraisal_sync）→ generate_payouts / void_payouts
  - `_recompute_settlement_special_total`（api/year_end）→ add_special_bonus

觸發鏈：Excel 匯入建 Excel 版 FESTIVAL_DIFF（並刪該員 auto 版）→ 之後
build_settlements(refresh_rates=True) 經 derive_all 重新 derive auto 版 → 兩列共存 →
HR 觸發 add_special_bonus 或 generate/void payout → 上述路徑以裸 SUM 重算 DRAFT
settlement.total_amount → 同一筆節慶差額 Excel 列 + auto 列各計一次 → total_amount 灌大 →
轉帳名冊多發。本測試固化「兩條 recompute 路徑與 build 同口徑」。
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
from api.year_end import _recompute_settlement_special_total
from services.year_end.appraisal_sync import _recompute_draft_settlement_total


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


def _add_excel_plus_auto_dup(session, cycle_id, emp_id):
    """同一 FESTIVAL_DIFF：Excel 列 1000 + auto 列 1000（重複），excel-wins 應只計 1000。"""
    _item(
        session,
        cycle_id,
        emp_id,
        SpecialBonusType.FESTIVAL_DIFF,
        "114-FD",
        1000,
        "auto:festival_diff",
    )
    _item(
        session,
        cycle_id,
        emp_id,
        SpecialBonusType.FESTIVAL_DIFF,
        "114.8-115.01",
        1000,
        "年終獎金總表",
    )


def test_recompute_draft_settlement_total_applies_excel_wins(test_db_session):
    """generate/void payout 觸發的 _recompute_draft_settlement_total 須去重（不雙計）。"""
    s = test_db_session
    cycle = _make_cycle(s)
    EMP = 41
    settlement = _make_draft_settlement(s, cycle.id, EMP, Decimal("30000"))
    _add_excel_plus_auto_dup(s, cycle.id, EMP)
    s.flush()

    _recompute_draft_settlement_total(s, cycle.id, EMP)

    assert settlement.special_bonus_total == Decimal("1000"), (
        "excel 列勝出、同型 auto 列排除 → special_bonus_total=1000；"
        f"實際 {settlement.special_bonus_total}（2000=Excel+auto 雙計 → 轉帳多發）"
    )
    assert settlement.total_amount == Decimal(
        "31000"
    ), f"total_amount 應為 payable 30000 + special 1000 = 31000，實際 {settlement.total_amount}"


def test_recompute_settlement_special_total_applies_excel_wins(test_db_session):
    """add_special_bonus 觸發的 _recompute_settlement_special_total 須去重（不雙計）。"""
    s = test_db_session
    cycle = _make_cycle(s)
    EMP = 42
    settlement = _make_draft_settlement(s, cycle.id, EMP, Decimal("30000"))
    _add_excel_plus_auto_dup(s, cycle.id, EMP)
    s.flush()

    _recompute_settlement_special_total(s, cycle.id, EMP)

    assert settlement.special_bonus_total == Decimal("1000"), (
        "excel 列勝出、同型 auto 列排除 → special_bonus_total=1000；"
        f"實際 {settlement.special_bonus_total}（2000=Excel+auto 雙計 → 轉帳多發）"
    )
    assert settlement.total_amount == Decimal(
        "31000"
    ), f"total_amount 應為 payable 30000 + special 1000 = 31000，實際 {settlement.total_amount}"


def test_recompute_draft_auto_only_still_counted(test_db_session):
    """無 Excel 列時（純 auto）兩條路徑維持全計，去重不得誤殺正常 auto 獎金。"""
    s = test_db_session
    cycle = _make_cycle(s)
    EMP = 43
    settlement = _make_draft_settlement(s, cycle.id, EMP, Decimal("30000"))
    _item(
        s,
        cycle.id,
        EMP,
        SpecialBonusType.FESTIVAL_DIFF,
        "114-FD",
        1000,
        "auto:festival_diff",
    )
    s.flush()

    _recompute_draft_settlement_total(s, cycle.id, EMP)
    assert settlement.special_bonus_total == Decimal("1000")
    assert settlement.total_amount == Decimal("31000")


def test_recompute_special_auto_only_still_counted(test_db_session):
    """add_special_bonus 路徑：純 auto 仍全計。"""
    s = test_db_session
    cycle = _make_cycle(s)
    EMP = 44
    settlement = _make_draft_settlement(s, cycle.id, EMP, Decimal("30000"))
    _item(
        s,
        cycle.id,
        EMP,
        SpecialBonusType.FESTIVAL_DIFF,
        "114-FD",
        1000,
        "auto:festival_diff",
    )
    s.flush()

    _recompute_settlement_special_total(s, cycle.id, EMP)
    assert settlement.special_bonus_total == Decimal("1000")
    assert settlement.total_amount == Decimal("31000")
