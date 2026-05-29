"""BE-P2-1：批次預載 helper 的 batch == single 等價性測試。

每個 batch helper 必須對每位員工回傳與 per-employee 版本「逐位完全相同」的值
（值不變是這次效能重構的鐵律）。涵蓋「有資料」與「無資料」兩位員工，並對
appraisal 驗證 2 月與非 2 月。
"""

from datetime import date
from decimal import Decimal

import pytest

from models.employee import Employee
from models.salary import SalaryRecord
from models.year_end import SpecialBonusItem, SpecialBonusType, YearEndCycle
from models.leave import LeaveRecord


@pytest.fixture
def two_employees(test_db_session):
    a = Employee(employee_id="BULK_A", name="員工A", is_active=True)
    b = Employee(employee_id="BULK_B", name="員工B", is_active=True)
    test_db_session.add_all([a, b])
    test_db_session.flush()
    return a, b


# ── #2 ytd_bonus ─────────────────────────────────────────────────────────────
def test_query_ytd_bonus_bulk_equals_single(test_db_session, two_employees):
    from services.salary.supplementary_premium import (
        query_ytd_bonus_before,
        query_ytd_bonus_bulk,
    )

    a, b = two_employees
    # A 有 1 月獎金（festival 1000 + overtime 500），B 無任何前月紀錄
    test_db_session.add(
        SalaryRecord(
            employee_id=a.id,
            salary_year=2026,
            salary_month=1,
            festival_bonus=1000,
            overtime_bonus=500,
        )
    )
    test_db_session.flush()

    bulk = query_ytd_bonus_bulk(test_db_session, [a.id, b.id], 2026, 2)
    assert bulk[a.id] == query_ytd_bonus_before(test_db_session, a.id, 2026, 2)
    assert bulk[b.id] == query_ytd_bonus_before(test_db_session, b.id, 2026, 2)
    assert bulk[a.id] == 1500.0
    assert bulk[b.id] == 0.0


# ── #1 appraisal ─────────────────────────────────────────────────────────────
@pytest.fixture
def appraisal_setup(test_db_session, two_employees):
    a, b = two_employees
    cycle = YearEndCycle(
        academic_year=114,  # 民國 2026 → 114
        start_date=date(2025, 8, 1),
        end_date=date(2026, 7, 31),
        bonus_calc_date=date(2026, 1, 15),
    )
    test_db_session.add(cycle)
    test_db_session.flush()
    # A 兩筆（6400 + 7200 = 13600）；B 無
    test_db_session.add_all(
        [
            SpecialBonusItem(
                year_end_cycle_id=cycle.id,
                employee_id=a.id,
                bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST,
                period_label="113下",
                amount=Decimal("6400"),
            ),
            SpecialBonusItem(
                year_end_cycle_id=cycle.id,
                employee_id=a.id,
                bonus_type=SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND,
                period_label="114上",
                amount=Decimal("7200"),
            ),
        ]
    )
    test_db_session.flush()
    return a, b


def test_appraisal_bulk_equals_single_february(test_db_session, appraisal_setup):
    from services.salary.appraisal_year_end import (
        query_appraisal_year_end_bonus,
        query_appraisal_year_end_bonus_bulk,
    )

    a, b = appraisal_setup
    bulk = query_appraisal_year_end_bonus_bulk(test_db_session, [a.id, b.id], 2026, 2)
    assert bulk[a.id] == query_appraisal_year_end_bonus(test_db_session, a.id, 2026, 2)
    assert bulk[b.id] == query_appraisal_year_end_bonus(test_db_session, b.id, 2026, 2)
    # 型別必須是 Decimal（直接寫進 column 並進下月累計）
    assert isinstance(bulk[a.id], Decimal)
    assert bulk[a.id] == Decimal("13600")
    assert bulk[b.id] == Decimal("0")


def test_appraisal_bulk_zero_for_non_february(test_db_session, appraisal_setup):
    from services.salary.appraisal_year_end import query_appraisal_year_end_bonus_bulk

    a, b = appraisal_setup
    bulk = query_appraisal_year_end_bonus_bulk(test_db_session, [a.id, b.id], 2026, 6)
    assert bulk[a.id] == Decimal("0")
    assert bulk[b.id] == Decimal("0")


# ── #4 skip bonuses ──────────────────────────────────────────────────────────
def test_should_skip_bonuses_bulk_equals_single(test_db_session, two_employees):
    from services.leave_bonus_skip import (
        should_skip_bonuses_bulk,
        should_skip_bonuses_for_month,
    )

    a, b = two_employees
    # A：產假橫跨 2025-12 與 2026-01（兩個 period 月，郭玟秀 case）；B：無
    test_db_session.add(
        LeaveRecord(
            employee_id=a.id,
            leave_type="maternity",
            start_date=date(2025, 12, 10),
            end_date=date(2026, 1, 20),
            leave_hours=0,
            status="approved",
        )
    )
    test_db_session.flush()

    pairs = [(2025, 12), (2026, 1)]
    bulk = should_skip_bonuses_bulk(test_db_session, [a.id, b.id], pairs)
    for eid in (a.id, b.id):
        for y, m in pairs:
            single_skip, _ = should_skip_bonuses_for_month(test_db_session, eid, y, m)
            assert bulk[(eid, y, m)] == single_skip, f"emp={eid} {y}-{m}"
    # A 兩個月都 skip；B 都不 skip
    assert bulk[(a.id, 2025, 12)] is True
    assert bulk[(a.id, 2026, 1)] is True
    assert bulk[(b.id, 2025, 12)] is False
    assert bulk[(b.id, 2026, 1)] is False
