"""驗證 snapshot_leave step：寫 leave_balance_snapshot JSONB + leave_snapshot_at。"""

import os
import sys
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, User, LeaveQuota
from models.offboarding import EmployeeOffboardingRecord
from models.salary import SalaryRecord

_counter = 0


@pytest.fixture
def db_session(tmp_path):
    """SQLite test session（對齊既有 offboarding step test pattern）。"""
    db_path = tmp_path / "offboarding_step_snapshot_leave.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    session = session_factory()
    yield session
    session.close()

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def employee_factory(db_session):
    """建立測試員工。daily_wage 換算為 base_salary = daily_wage * 30 存入 DB。"""

    def _factory(
        *,
        hire_date=date(2020, 1, 1),
        is_active=True,
        daily_wage=None,
    ) -> Employee:
        global _counter
        _counter += 1
        # Employee model 只有 base_salary；daily_wage 是測試便利參數，換算存入
        base_salary = int(daily_wage * 30) if daily_wage is not None else 0
        emp = Employee(
            employee_id=f"SLT{_counter:04d}",
            name=f"測試員工{_counter}",
            hire_date=hire_date,
            is_active=is_active,
            base_salary=base_salary,
        )
        db_session.add(emp)
        db_session.flush()
        return emp

    return _factory


@pytest.fixture
def user_factory(db_session):
    def _factory(*, role="admin") -> User:
        global _counter
        _counter += 1
        u = User(
            username=f"testuser{_counter}",
            password_hash="dummy",
            role=role,
        )
        db_session.add(u)
        db_session.flush()
        return u

    return _factory


@pytest.fixture
def leave_quota_factory(db_session):
    def _factory(
        *,
        employee_id: int,
        year: int,
        leave_type: str,
        total_hours: float,
        school_year=None,
    ) -> LeaveQuota:
        quota = LeaveQuota(
            employee_id=employee_id,
            year=year,
            leave_type=leave_type,
            total_hours=total_hours,
            school_year=school_year,
        )
        db_session.add(quota)
        db_session.flush()
        return quota

    return _factory


@pytest.fixture
def salary_record_factory(db_session):
    def _factory(
        *,
        employee_id: int,
        salary_year: int,
        salary_month: int,
    ) -> SalaryRecord:
        sr = SalaryRecord(
            employee_id=employee_id,
            salary_year=salary_year,
            salary_month=salary_month,
        )
        db_session.add(sr)
        db_session.flush()
        return sr

    return _factory


def _make_record(db_session, employee_id, user_id):
    record = EmployeeOffboardingRecord(
        employee_id=employee_id,
        resign_date=date(2026, 6, 15),
        opened_at=datetime.now(),
        opened_by_user_id=user_id,
    )
    db_session.add(record)
    db_session.flush()
    return record


def test_snapshot_writes_balance_to_jsonb(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    """snapshot run() 寫入正確 JSONB snapshot 及 leave_snapshot_at。"""
    emp = employee_factory(daily_wage=1800)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    record = _make_record(db_session, emp.id, user.id)

    from services.offboarding.steps.snapshot_leave import run

    result = run(db_session, record)

    assert result["step"] == "snapshot_leave"
    assert result["status"] == "completed"
    assert record.leave_snapshot_at is not None
    snap = record.leave_balance_snapshot
    assert snap["total_hours"] == 112.0
    assert snap["used_hours"] == 0.0
    assert snap["remaining_hours"] == 112.0
    assert snap["remaining_days"] == 14.0
    # daily_wage = base_salary / 30 = 54000 / 30 = 1800.0
    assert snap["daily_wage"] == 1800.0
    assert snap["payout_amount"] == 14 * 1800  # 25200
    assert snap["calc_rule_version"] == "labor_act_38_2026_v1"
    assert result["payload"] == {"days": 14.0, "payout": 25200.0}


def test_snapshot_when_no_quota_returns_zero(
    db_session, employee_factory, user_factory
):
    """無 quota row 不算失敗：員工可能剛到職未生 quota，snapshot 寫 0。"""
    emp = employee_factory(daily_wage=1500)
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)

    from services.offboarding.steps.snapshot_leave import run

    result = run(db_session, record)
    assert result["status"] == "completed"
    assert record.leave_balance_snapshot["remaining_days"] == 0.0
    assert record.leave_balance_snapshot["payout_amount"] == 0.0


def test_snapshot_raises_when_employee_has_no_daily_wage(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    """daily_wage 缺失（base_salary=0）→ raise OffboardingError LEAVE_BALANCE_NOT_FOUND。"""
    emp = employee_factory(
        daily_wage=None
    )  # base_salary=0 → _resolve_daily_wage 回 None
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    record = _make_record(db_session, emp.id, user.id)

    from services.offboarding.steps.snapshot_leave import run
    from services.offboarding.orchestrator import OffboardingError

    with pytest.raises(OffboardingError) as exc_info:
        run(db_session, record)
    assert exc_info.value.code == "LEAVE_BALANCE_NOT_FOUND"


def test_prefill_salary_writes_to_existing_record(
    db_session,
    employee_factory,
    user_factory,
    leave_quota_factory,
    salary_record_factory,
):
    """prefill_salary 把 snapshot.payout_amount 寫入既有 SalaryRecord.unused_leave_payout。"""
    emp = employee_factory(daily_wage=1800)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    record = _make_record(db_session, emp.id, user.id)
    sr = salary_record_factory(employee_id=emp.id, salary_year=2026, salary_month=6)

    from services.offboarding.steps.snapshot_leave import run, prefill_salary

    run(db_session, record)
    result = prefill_salary(db_session, record)

    assert result["status"] == "completed"
    assert result["payload"]["salary_record_id"] == sr.id
    db_session.refresh(sr)
    assert float(sr.unused_leave_payout) == 25200.0


def test_prefill_salary_skips_when_no_record(
    db_session, employee_factory, user_factory, leave_quota_factory
):
    """prefill_salary 當月無 SalaryRecord → status=skipped。"""
    emp = employee_factory(daily_wage=1500)
    user = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    record = _make_record(db_session, emp.id, user.id)

    from services.offboarding.steps.snapshot_leave import run, prefill_salary

    run(db_session, record)
    result = prefill_salary(db_session, record)

    assert result["status"] == "skipped"
    assert result["payload"]["reason"] == "salary_record_not_yet_created"
