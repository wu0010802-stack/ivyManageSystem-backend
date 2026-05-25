"""驗證 mark_appraisal step：純寫 timestamp，不會失敗。"""

import os
import sys
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, User
from models.offboarding import EmployeeOffboardingRecord
from services.offboarding.steps.mark_appraisal import run

_counter = 0


@pytest.fixture
def db_session(tmp_path):
    """SQLite test session（對齊 test_offboarding_orchestrator pattern）。"""
    db_path = tmp_path / "offboarding_step_mark_appraisal.sqlite"
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
    def _factory(
        *,
        hire_date=date(2020, 1, 1),
        is_active=True,
    ) -> Employee:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"OBT{_counter:04d}",
            name=f"測試員工{_counter}",
            hire_date=hire_date,
            is_active=is_active,
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


def test_mark_appraisal_writes_timestamp(db_session, employee_factory, user_factory):
    """驗證 mark_appraisal 寫入 appraisal_marked_at timestamp。"""
    emp = employee_factory()
    user = user_factory()
    record = EmployeeOffboardingRecord(
        employee_id=emp.id,
        resign_date=date(2026, 6, 15),
        opened_at=datetime.now(),
        opened_by_user_id=user.id,
    )
    db_session.add(record)
    db_session.flush()

    result = run(db_session, record)
    assert result["step"] == "mark_appraisal"
    assert result["status"] == "completed"
    assert result["completed_at"] is not None
    assert record.appraisal_marked_at is not None
