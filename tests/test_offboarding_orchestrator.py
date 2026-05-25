"""驗證 orchestrator types + 主入口 signature。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, User
from utils.auth import hash_password

from services.offboarding.orchestrator import (
    OffboardingError,
    OffboardingResult,
    StepResult,
    process_offboarding,
)

_counter = 0


@pytest.fixture
def db_session(tmp_path):
    """SQLite test session（inline — 對齊 test_leave_quota_helpers pattern）。"""
    db_path = tmp_path / "offboarding_orchestrator.sqlite"
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
            password_hash=hash_password("Passw0rd!"),
            role=role,
            is_active=True,
        )
        db_session.add(u)
        db_session.flush()
        return u

    return _factory


# ── tests ──────────────────────────────────────────────────────────────────


def test_step_result_typeddict_fields():
    sr: StepResult = {
        "step": "mark_appraisal",
        "status": "completed",
        "completed_at": None,
        "payload": None,
        "error": None,
    }
    assert sr["step"] == "mark_appraisal"


def test_offboarding_error_subclass_of_exception():
    err = OffboardingError("test", code="LEAVE_BALANCE_NOT_FOUND")
    assert isinstance(err, Exception)
    assert err.code == "LEAVE_BALANCE_NOT_FOUND"


def test_process_offboarding_creates_record_with_only_default_step_unimplemented(
    db_session, employee_factory, user_factory, monkeypatch
):
    """初版 orchestrator 殼：無 step 註冊 → 建 record + 空 steps list。
    後續 task 加 step 後此測試會被覆寫；現在只驗框架可呼叫。"""
    emp = employee_factory()
    user = user_factory()
    result = process_offboarding(
        session=db_session,
        employee_id=emp.id,
        resign_date=date(2026, 6, 15),
        resign_reason="test",
        operator_user_id=user.id,
    )
    assert result["employee_id"] == emp.id
    assert result["resign_date"] == date(2026, 6, 15)
    assert isinstance(result["steps"], list)

    from models.offboarding import EmployeeOffboardingRecord

    record = (
        db_session.query(EmployeeOffboardingRecord)
        .filter_by(employee_id=emp.id)
        .first()
    )
    assert record is not None
    assert record.opened_by_user_id == user.id
