"""驗證 get_annual_leave_balance 公式 = quota.total_hours - approved_used_hours。

注意：LeaveRecord 使用 is_approved（boolean nullable）欄位：
  True  → approved
  None  → pending
  False → rejected
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, LeaveQuota, LeaveRecord

from utils.leave_quota_helpers import get_annual_leave_balance

_emp_counter = 0


@pytest.fixture
def db_session(tmp_path):
    """SQLite in-memory test session（同 test_leave_quota_cutover.py pattern）。"""
    db_path = tmp_path / "leave_quota_helpers.sqlite"
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


def _make_employee(db_session, name: str = "測試員工") -> Employee:
    global _emp_counter
    _emp_counter += 1
    emp = Employee(
        employee_id=f"LQHT{_emp_counter:04d}",
        name=name,
        hire_date=date(2020, 1, 1),
    )
    db_session.add(emp)
    db_session.flush()
    return emp


def _make_quota(
    db_session,
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


def _make_leave_record(
    db_session,
    employee_id: int,
    leave_type: str,
    start_date: date,
    end_date: date,
    leave_hours: float,
    status: str = "approved",  # "approved"/"pending"/"rejected"
) -> LeaveRecord:
    record = LeaveRecord(
        employee_id=employee_id,
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        leave_hours=leave_hours,
        status=status,
    )
    db_session.add(record)
    db_session.flush()
    return record


def test_returns_zero_when_no_quota(db_session):
    emp = _make_employee(db_session)
    result = get_annual_leave_balance(db_session, emp.id, date(2026, 6, 15))
    assert result == {
        "total_hours": 0.0,
        "used_hours": 0.0,
        "remaining_hours": 0.0,
        "remaining_days": 0.0,
        "snapshot_date": date(2026, 6, 15),
    }


def test_calculates_remaining_from_quota_minus_approved(db_session):
    emp = _make_employee(db_session)
    _make_quota(db_session, emp.id, 2026, "annual", total_hours=112)  # 14 天
    _make_leave_record(
        db_session,
        emp.id,
        "annual",
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 9),
        leave_hours=72,  # 9 天
        status="approved",  # approved
    )
    result = get_annual_leave_balance(db_session, emp.id, date(2026, 6, 15))
    assert result["total_hours"] == 112.0
    assert result["used_hours"] == 72.0
    assert result["remaining_hours"] == 40.0
    assert result["remaining_days"] == 5.0


def test_excludes_pending_records(db_session):
    """只算 approved；pending（status="pending"）不扣——離職時 pending 假應由 admin 處理。"""
    emp = _make_employee(db_session)
    _make_quota(db_session, emp.id, 2026, "annual", total_hours=80)
    _make_leave_record(
        db_session,
        emp.id,
        "annual",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 1),
        leave_hours=8,
        status="pending",  # pending
    )
    result = get_annual_leave_balance(db_session, emp.id, date(2026, 6, 15))
    assert result["used_hours"] == 0.0
    assert result["remaining_hours"] == 80.0


def test_uses_school_year_row_when_present(db_session):
    """同 employee 同 year 有 legacy（school_year=None）+ school_year row 時，school_year row 優先。"""
    emp = _make_employee(db_session)
    _make_quota(
        db_session, emp.id, 2026, "annual", total_hours=80, school_year=None
    )  # legacy row
    _make_quota(
        db_session, emp.id, 2026, "annual", total_hours=112, school_year=115
    )  # 民國 115 = 2026 學年
    result = get_annual_leave_balance(db_session, emp.id, date(2026, 6, 15))
    assert result["total_hours"] == 112.0  # school_year row 優先
