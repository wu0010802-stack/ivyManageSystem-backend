"""本批 race condition 修補的回歸測試（2026-05-16）。

覆蓋：
- P0-3 add_special_bonus 拒絕 FINALIZED settlement
- P0-3 反向 race：special_bonus 先於 settlement 建立 → 後建 settlement 必須帶入既有總額
- P0-3 excel_io UPDATE path 跳過 FINALIZED settlement
- P1-7 salary_snapshots month_end/finalize partial unique 真的擋下重複插入
- P1-7 create_month_end_snapshots 第二次 idempotent，不會把第一次的 race-lost 變 IntegrityError
- P2-12 lazy guard 早退時 pop key，同日後續呼叫可重試
- P2-13 SalaryCalcJob 同 (year, month) active 第二筆觸發 IntegrityError → ActiveJobExistsError
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from models.fees import StudentFeeRecord  # noqa: F401 — 註冊 Base.metadata
from models.salary import SalaryRecord, SalarySnapshot, SalaryCalcJobRecord
from models.year_end import (
    EmployeeYearEndSnapshot,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
    YearEndSettlement,
    YearEndSettlementStatus,
)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


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


def _make_snapshot(session, cycle_id: int, employee_id: int) -> EmployeeYearEndSnapshot:
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
    return snap


def _make_settlement(
    session,
    cycle_id: int,
    employee_id: int = 1,
    status: YearEndSettlementStatus = YearEndSettlementStatus.DRAFT,
    payable: Decimal = Decimal("30000"),
) -> YearEndSettlement:
    snap = _make_snapshot(session, cycle_id, employee_id)
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


# ─────────────────────────────────────────────────────────────────────────────
# P0-3: settlement FINALIZED guard
# ─────────────────────────────────────────────────────────────────────────────


def test_recompute_special_total_refuses_finalized(test_db_session):
    """`_recompute_settlement_special_total` 對 FINALIZED settlement 應拋 HTTPException。"""
    from fastapi import HTTPException

    from api.year_end import _recompute_settlement_special_total

    session = test_db_session
    cycle = _make_cycle(session)
    _make_settlement(
        session,
        cycle.id,
        employee_id=42,
        status=YearEndSettlementStatus.FINALIZED,
        payable=Decimal("28000"),
    )
    session.add(
        SpecialBonusItem(
            year_end_cycle_id=cycle.id,
            employee_id=42,
            bonus_type=SpecialBonusType.CUSTOM,
            period_label="紅包",
            amount=Decimal("5000"),
        )
    )
    session.flush()

    with pytest.raises(HTTPException) as exc:
        _recompute_settlement_special_total(session, cycle.id, 42)
    assert exc.value.status_code == 400
    assert "FINALIZED" in exc.value.detail


def test_recompute_special_total_noop_when_settlement_missing(test_db_session):
    """settlement 還沒建立時不要拋例外（後續 import_excel 會自我修補）。"""
    from api.year_end import _recompute_settlement_special_total

    session = test_db_session
    cycle = _make_cycle(session)
    # 不建 settlement，直接呼叫
    _recompute_settlement_special_total(session, cycle.id, employee_id=99)
    # 無 exception 即過


def test_excel_io_settlement_creation_picks_up_existing_special_bonus(
    test_db_session,
):
    """反向 race：先放 special_bonus，後跑 import_year_end_to_db 建 settlement，
    total_amount 必須含先前 special_bonus_total（即使本次 Excel 沒帶該員特別獎金）。"""
    from services.year_end.excel_io import (
        ParsedSettlementRow,
        ParsedYearEndExcel,
        import_year_end_to_db,
    )

    session = test_db_session
    # 不預建 cycle，讓 import_year_end_to_db 自己建立
    EMP_ID = 7
    # 先建立空白 cycle（import 會 reuse）；special_bonus FK 需要 cycle_id
    cycle = _make_cycle(session)
    session.add(
        SpecialBonusItem(
            year_end_cycle_id=cycle.id,
            employee_id=EMP_ID,
            bonus_type=SpecialBonusType.CUSTOM,
            period_label="預發紅包",
            amount=Decimal("3500"),
        )
    )
    session.commit()

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
        special_bonuses=[],
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
    assert settlement.special_bonus_total == Decimal("3500")
    assert settlement.total_amount == settlement.payable_amount + Decimal("3500")


def test_excel_io_update_path_skips_finalized_settlement(test_db_session):
    """FINALIZED settlement 在 import_excel 再跑一次時應跳過更新，避免覆寫轉帳金額。"""
    from services.year_end.excel_io import (
        ParsedSettlementRow,
        ParsedYearEndExcel,
        import_year_end_to_db,
    )

    session = test_db_session
    cycle = _make_cycle(session)
    EMP_ID = 11
    s = _make_settlement(
        session,
        cycle.id,
        employee_id=EMP_ID,
        status=YearEndSettlementStatus.FINALIZED,
        payable=Decimal("28000"),
    )
    s.total_amount = Decimal("28000")
    session.commit()

    parsed = ParsedYearEndExcel(
        academic_year=114,
        settlements=[
            ParsedSettlementRow(
                excel_row=1,
                name="王測試",
                base_salary=Decimal("99999"),
                festival_total=Decimal("0"),
                avg_performance_rate=Decimal("90"),
                gross_amount=Decimal("99999"),
                org_achievement_rate=Decimal("100"),
                subtotal=Decimal("99999"),
                total_in_year=Decimal("12"),
                payable=Decimal("99999"),
            ),
        ],
        special_bonuses=[],
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
    assert settlement.payable_amount == Decimal(
        "28000"
    ), "FINALIZED settlement 不應被 import_excel 覆寫"
    assert settlement.base_salary == Decimal("30000")


# ─────────────────────────────────────────────────────────────────────────────
# P1-7: SalarySnapshot partial unique
# ─────────────────────────────────────────────────────────────────────────────


def _make_salary_snapshot_kwargs(emp_id: int, year: int, month: int, type_: str):
    return dict(
        employee_id=emp_id,
        salary_year=year,
        salary_month=month,
        snapshot_type=type_,
        captured_at=datetime.now(),
        captured_by="test",
        base_salary=30000,
    )


def test_salary_snapshot_month_end_partial_unique_blocks_duplicate(test_db_session):
    """同 (emp, year, month) 兩筆 month_end 應觸發 IntegrityError。"""
    session = test_db_session
    s1 = SalarySnapshot(**_make_salary_snapshot_kwargs(1, 2026, 5, "month_end"))
    session.add(s1)
    session.commit()

    s2 = SalarySnapshot(**_make_salary_snapshot_kwargs(1, 2026, 5, "month_end"))
    session.add(s2)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_salary_snapshot_manual_allows_duplicate(test_db_session):
    """manual 類型允許重複（管理員可手動補拍）。"""
    session = test_db_session
    s1 = SalarySnapshot(**_make_salary_snapshot_kwargs(2, 2026, 5, "manual"))
    s2 = SalarySnapshot(**_make_salary_snapshot_kwargs(2, 2026, 5, "manual"))
    session.add_all([s1, s2])
    session.commit()
    rows = (
        session.query(SalarySnapshot)
        .filter_by(employee_id=2, salary_year=2026, salary_month=5)
        .all()
    )
    assert len(rows) == 2


def test_create_month_end_snapshots_idempotent_under_race(test_db_session):
    """第二次呼叫不應 raise；savepoint + IntegrityError 接住既有 row。"""
    from models.employee import Employee
    from services.salary_snapshot_service import create_month_end_snapshots

    session = test_db_session
    emp = Employee(
        employee_id="E_RC",
        name="race emp",
        title="幼兒園教師",
        position="幼兒園教師",
        employee_type="regular",
        base_salary=30000,
        hire_date=date(2025, 1, 1),
        is_active=True,
    )
    session.add(emp)
    session.flush()
    record = SalaryRecord(
        employee_id=emp.id, salary_year=2026, salary_month=4, base_salary=30000
    )
    session.add(record)
    session.commit()

    n1 = create_month_end_snapshots(session, 2026, 4, "test1")
    session.commit()
    assert n1 == 1

    # 模擬另一 worker 同秒撞：直接重呼，且預期不會炸
    n2 = create_month_end_snapshots(session, 2026, 4, "test2")
    session.commit()
    assert n2 == 0  # 已存在 → 沒新增


# ─────────────────────────────────────────────────────────────────────────────
# P2-12: lazy guard 早退釋放
# ─────────────────────────────────────────────────────────────────────────────


def test_lazy_snapshot_guard_releases_on_early_return(test_db_session, monkeypatch):
    """無 SalaryRecord 時應釋放 guard key，同日後續再呼叫應可重試。"""
    from fastapi import BackgroundTasks

    from api.salary import (
        _snapshot_lazy_guard,
        _trigger_past_month_snapshot_if_missing,
    )

    _snapshot_lazy_guard.clear()

    bg = BackgroundTasks()
    # 第一次呼叫：DB 無 record → 走 early-return
    _trigger_past_month_snapshot_if_missing(bg)
    assert (
        len(_snapshot_lazy_guard) == 0
    ), f"guard key 應在早退時釋放，但留下 {_snapshot_lazy_guard}"

    # 第二次呼叫（同日同 ym）：因為 key 已釋放，仍會嘗試
    bg2 = BackgroundTasks()
    _trigger_past_month_snapshot_if_missing(bg2)
    assert len(_snapshot_lazy_guard) == 0


# ─────────────────────────────────────────────────────────────────────────────
# P2-13: SalaryCalcJob active partial unique
# ─────────────────────────────────────────────────────────────────────────────


def test_salary_calc_jobs_active_unique_blocks_duplicate(test_db_session):
    """同 (year, month) 第二筆 active job 應觸發 IntegrityError。"""
    session = test_db_session
    j1 = SalaryCalcJobRecord(
        job_id="aaa111",
        year=2026,
        month=5,
        status="pending",
        total=10,
    )
    j2 = SalaryCalcJobRecord(
        job_id="bbb222",
        year=2026,
        month=5,
        status="running",
        total=10,
    )
    session.add(j1)
    session.commit()
    session.add(j2)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_salary_calc_jobs_completed_allows_new(test_db_session):
    """completed/failed 不算 active，可以再開新 job。"""
    session = test_db_session
    j1 = SalaryCalcJobRecord(
        job_id="ccc333",
        year=2026,
        month=6,
        status="completed",
        total=10,
    )
    session.add(j1)
    session.commit()

    j2 = SalaryCalcJobRecord(
        job_id="ddd444",
        year=2026,
        month=6,
        status="pending",
        total=10,
    )
    session.add(j2)
    session.commit()  # 不應炸


# ─────────────────────────────────────────────────────────────────────────────
# 非月費 fee_records partial unique（P2-3）
# ─────────────────────────────────────────────────────────────────────────────


def test_fee_records_non_monthly_partial_unique_blocks_duplicate(test_db_session):
    """同 (student, source_template, period) 且 target_month=NULL 第二筆應觸發 IntegrityError。"""
    from models.fees import StudentFeeRecord

    session = test_db_session
    base_kwargs = dict(
        student_id=1,
        student_name="王測試",
        classroom_name="大班",
        fee_item_name="註冊費",
        amount_due=12000,
        amount_paid=0,
        status="unpaid",
        fee_type="registration",
        source_template_id=99,
        target_month=None,
        period="115-1",
    )
    r1 = StudentFeeRecord(**base_kwargs)
    r2 = StudentFeeRecord(**base_kwargs)
    session.add(r1)
    session.commit()
    session.add(r2)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_fee_records_non_monthly_unique_allows_different_period(test_db_session):
    """同 (student, source_template) 不同 period 不應衝突。"""
    from models.fees import StudentFeeRecord

    session = test_db_session
    base = dict(
        student_id=2,
        student_name="王測試二",
        classroom_name="大班",
        fee_item_name="註冊費",
        amount_due=12000,
        amount_paid=0,
        status="unpaid",
        fee_type="registration",
        source_template_id=88,
        target_month=None,
    )
    r1 = StudentFeeRecord(**base, period="115-1")
    r2 = StudentFeeRecord(**base, period="115-2")
    session.add_all([r1, r2])
    session.commit()  # 不應炸
