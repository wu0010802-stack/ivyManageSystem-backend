"""特休週年 cutover 行為驗證：5 測試案例涵蓋主路徑與邊界。"""

import itertools
import pytest
from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sqlalchemy as _sa
import sqlalchemy.sql.sqltypes as _sqltypes
import sqlalchemy.dialects.postgresql as _pg_dialects
from sqlalchemy import JSON as _JSON

# SQLite 相容性修補（與 conftest.py 同步）
_pg_dialects.JSONB = _JSON  # type: ignore[assignment]


class _SQLiteInteger(_sa.Integer):  # type: ignore[misc]
    """SQLite 相容的 BigInteger 替代型別。"""

    pass


_sa.BigInteger = _SQLiteInteger  # type: ignore[assignment]
_sqltypes.BigInteger = _SQLiteInteger  # type: ignore[assignment]


@pytest.fixture
def session(tmp_path):
    """SQLite in-memory 測試 DB，建立全 schema。"""
    from models.database import Base  # noqa: F401 — 觸發核心 model 的 metadata 注冊

    # 明確注冊本測試依賴的 model
    import models.unused_leave_payout_log  # noqa: F401

    db_path = tmp_path / "test_annual_cutover.sqlite"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    test_session_factory = sessionmaker(bind=test_engine)
    Base.metadata.create_all(test_engine)

    sess = test_session_factory()
    yield sess
    sess.close()
    test_engine.dispose()


# 用於產生唯一員工工號
_emp_id_counter = itertools.count(1)


@pytest.fixture
def employee_factory(session):
    """輕量員工 factory。

    預設值：is_active=True, employee_type='regular', base_salary=30000。
    """
    from models.employee import Employee

    def _make(
        employee_type="regular",
        base_salary=30000.0,
        hourly_rate=0.0,
        is_active=True,
        hire_date=None,
    ):
        n = next(_emp_id_counter)
        emp = Employee(
            employee_id=f"E{n:04d}",
            name=f"測試員工{n}",
            employee_type=employee_type,
            base_salary=base_salary,
            hourly_rate=hourly_rate,
            is_active=is_active,
            hire_date=hire_date or date(2020, 1, 1),
        )
        session.add(emp)
        session.flush()
        return emp

    return _make


@pytest.fixture
def salary_record_factory(session):
    """輕量 SalaryRecord factory。"""
    from models.salary import SalaryRecord

    def _make(emp_id, year, month, is_finalized=False, unused_leave_payout=0):
        sr = SalaryRecord(
            employee_id=emp_id,
            salary_year=year,
            salary_month=month,
            base_salary=30000,
            is_finalized=is_finalized,
            unused_leave_payout=Decimal(str(unused_leave_payout)),
        )
        session.add(sr)
        session.flush()
        return sr

    return _make


@pytest.fixture
def leave_quota_factory(session):
    """輕量 LeaveQuota factory。

    預設 school_year=None（週年制），leave_type='annual'。
    """
    from models.leave import LeaveQuota

    def _make(
        employee_id,
        leave_type="annual",
        total_hours=120.0,
        period_start=None,
        period_end=None,
        year=None,
        school_year=None,
    ):
        q = LeaveQuota(
            employee_id=employee_id,
            year=year or (period_start.year if period_start else date.today().year),
            school_year=school_year,
            leave_type=leave_type,
            total_hours=total_hours,
            period_start=period_start,
            period_end=period_end,
        )
        session.add(q)
        session.flush()
        return q

    return _make


# ──────────────────────────────────────────────────────────────────────────────
# 測試案例
# ──────────────────────────────────────────────────────────────────────────────


def test_cutover_no_anniversaries_no_op(session):
    """無人滿週年 → 無操作，total_anniversaries = 0。"""
    from services.leave_quota_expiry.annual_cutover import (
        cutover_annual_leave_anniversaries,
    )

    summary = cutover_annual_leave_anniversaries(date(2026, 4, 1), session)
    assert summary["total_anniversaries"] == 0


def test_cutover_cold_start_employee_creates_first_period_no_payout(
    session, employee_factory
):
    """員工 hire_date 月日 = today，且無既有 period_start row → cold start，
    建 new period 但無未休結算。

    hire_date=2020-04-01, today=2026-04-01 → 年資 6 年 → 120 hr
    """
    emp = employee_factory(
        hire_date=date(2020, 4, 1),
        employee_type="monthly",
        base_salary=48000.0,
    )

    from services.leave_quota_expiry.annual_cutover import (
        cutover_annual_leave_anniversaries,
    )

    summary = cutover_annual_leave_anniversaries(date(2026, 4, 1), session)

    assert summary["cold_start_employees"] == 1
    assert summary["paid_employees"] == 0

    from models.leave import LeaveQuota

    quota = (
        session.query(LeaveQuota)
        .filter_by(employee_id=emp.id, leave_type="annual")
        .first()
    )
    assert quota.period_start == date(2026, 4, 1)
    assert quota.period_end == date(2027, 4, 1)
    # 年資 6 年（hire 2020-04-01, ref 2026-04-01 → 72 months = 6y）→ 120 hr
    assert quota.total_hours == 120.0


def test_cutover_existing_period_with_unused_writes_log_and_new_period(
    session, employee_factory, leave_quota_factory
):
    """既有 period 有未休 → 寫 log + 建新 period。

    total_hours=120.0, used=0 → unused=120
    hourly_rate=200 → amount=120*200=24000
    """
    emp = employee_factory(
        hire_date=date(2020, 4, 1),
        employee_type="hourly",
        hourly_rate=200.0,
    )
    leave_quota_factory(
        employee_id=emp.id,
        leave_type="annual",
        total_hours=120.0,
        period_start=date(2025, 4, 1),
        period_end=date(2026, 4, 1),
    )

    from services.leave_quota_expiry.annual_cutover import (
        cutover_annual_leave_anniversaries,
    )

    summary = cutover_annual_leave_anniversaries(date(2026, 4, 1), session)

    assert summary["paid_employees"] == 1
    # 未用 = 120, amount = 120 * 200 = 24000
    assert summary["total_amount"] == 24000.0

    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    log = session.query(UnusedLeavePayoutLog).first()
    assert log.source_type == "annual_anniversary"
    assert log.amount == Decimal("24000.00")
    assert log.meta["period_start"] == "2025-04-01"

    from models.leave import LeaveQuota

    new_period = (
        session.query(LeaveQuota)
        .filter_by(employee_id=emp.id, period_start=date(2026, 4, 1))
        .first()
    )
    assert new_period is not None
    assert new_period.period_end == date(2027, 4, 1)


def test_cutover_idempotent_second_run_same_day_skips(session, employee_factory):
    """同日重跑 → IntegrityError 被吃掉，無重複 row。"""
    emp = employee_factory(hire_date=date(2020, 4, 1), base_salary=48000.0)

    from services.leave_quota_expiry.annual_cutover import (
        cutover_annual_leave_anniversaries,
    )

    cutover_annual_leave_anniversaries(date(2026, 4, 1), session)
    cutover_annual_leave_anniversaries(date(2026, 4, 1), session)  # 第二次

    from models.leave import LeaveQuota

    quotas = (
        session.query(LeaveQuota)
        .filter_by(employee_id=emp.id, leave_type="annual")
        .all()
    )
    assert len(quotas) == 1  # 不重複


def test_cutover_skips_employee_under_six_months(session, employee_factory):
    """未滿 180 天員工不 cutover。

    hire_date=2025-12-01, today=2026-04-01 → 約 121 天 < 180 天 → 跳過
    """
    today = date(2026, 4, 1)
    employee_factory(hire_date=date(2025, 12, 1))  # 約 4 個月

    from services.leave_quota_expiry.annual_cutover import (
        cutover_annual_leave_anniversaries,
    )

    summary = cutover_annual_leave_anniversaries(today, session)
    assert summary["total_anniversaries"] == 0
