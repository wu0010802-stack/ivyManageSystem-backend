"""驗證 revoke_user step 行為等價於 api/employees.py:783-806。"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, User
from models.offboarding import EmployeeOffboardingRecord

from services.offboarding.steps.revoke_user import run

_counter = 0


@pytest.fixture
def db_session(tmp_path):
    """SQLite test session（對齊既有 offboarding step test pattern）。"""
    db_path = tmp_path / "offboarding_step_revoke_user.sqlite"
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
            employee_id=f"RVK{_counter:04d}",
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
    def _factory(
        *,
        role="admin",
        employee_id=None,
        is_active=True,
        token_version=0,
    ) -> User:
        global _counter
        _counter += 1
        u = User(
            username=f"testuser{_counter}",
            password_hash="dummy",
            role=role,
            employee_id=employee_id,
            is_active=is_active,
            token_version=token_version,
        )
        db_session.add(u)
        db_session.flush()
        return u

    return _factory


def _make_record(db_session, employee_id, user_id, resign_date):
    record = EmployeeOffboardingRecord(
        employee_id=employee_id,
        resign_date=resign_date,
        opened_at=datetime.now(),
        opened_by_user_id=user_id,
    )
    db_session.add(record)
    db_session.flush()
    return record


def test_revokes_when_resign_date_is_today(db_session, employee_factory, user_factory):
    """resign_date == today → User.is_active=False，token_version++，user_revoked_at 寫入。"""
    emp = employee_factory()
    admin = user_factory()
    user = user_factory(employee_id=emp.id, is_active=True, token_version=3)
    record = _make_record(db_session, emp.id, admin.id, date.today())

    result = run(db_session, record)

    assert result["step"] == "revoke_user"
    assert result["status"] == "completed"
    assert result["payload"]["username"] == user.username
    assert result["payload"]["new_token_version"] == 4
    assert result["error"] is None
    # 同一 session identity map 內直接可見；refresh 前需 flush 否則讀回 DB 舊值
    db_session.flush()
    assert user.is_active is False
    assert user.token_version == 4
    assert record.user_revoked_at is not None


def test_skips_when_resign_date_future(db_session, employee_factory, user_factory):
    """通知期（resign_date > today）保留 User active 直到當日 cron 自動轉。"""
    emp = employee_factory()
    admin = user_factory()
    user = user_factory(employee_id=emp.id, is_active=True, token_version=3)
    record = _make_record(
        db_session, emp.id, admin.id, date.today() + timedelta(days=14)
    )

    result = run(db_session, record)

    assert result["step"] == "revoke_user"
    assert result["status"] == "skipped"
    assert result["payload"]["reason"] == "notice_period"
    assert result["error"] is None
    db_session.refresh(user)
    assert user.is_active is True
    assert user.token_version == 3


def test_completed_with_no_user_when_employee_never_had_account(
    db_session, employee_factory, user_factory
):
    """員工沒有對應 User 帳號 → completed with no_active_user note，不爆。"""
    emp = employee_factory()
    admin = user_factory()
    # 不建 user FK（emp 沒有關聯 User）
    record = _make_record(db_session, emp.id, admin.id, date.today())

    result = run(db_session, record)

    assert result["step"] == "revoke_user"
    assert result["status"] == "completed"
    assert result["payload"]["username"] is None
    assert result["payload"]["note"] == "no_active_user"
    assert result["error"] is None
    assert record.user_revoked_at is not None


def test_revoke_user_revokes_staff_refresh_family(
    db_session, employee_factory, user_factory
):
    """R6-4：撤帳須撤該 user 所有 active staff_refresh family（is_active=False 雖即時
    擋 /refresh，但未撤的 family 在 re-enable 未 bump token_version 時會復活）。"""
    from datetime import timedelta
    from models.staff_refresh_token import StaffRefreshToken

    emp = employee_factory()
    admin = user_factory()
    user = user_factory(employee_id=emp.id, is_active=True)
    tok = StaffRefreshToken(
        user_id=user.id,
        token_hash="dummyhash_" + "y" * 40,
        expires_at=datetime.now() + timedelta(days=30),
    )
    db_session.add(tok)
    db_session.flush()
    record = _make_record(db_session, emp.id, admin.id, date.today())

    run(db_session, record)

    db_session.refresh(tok)
    assert tok.revoked_at is not None, "撤帳須撤 staff_refresh family"


def test_scheduler_revokes_due_past_resign(db_session, employee_factory, user_factory):
    """R6-3：offboarding revoke scheduler 補撤 resign_date<=today 但 User 仍 active
    （user_revoked_at IS NULL）的離職記錄——落實 revoke_user docstring 宣稱的 cron。"""
    from datetime import timedelta
    from models.auth import User as _User
    from services.offboarding.offboarding_revoke_scheduler import (
        run_offboarding_revoke_due_once,
    )

    emp = employee_factory()
    admin = user_factory()
    user = user_factory(employee_id=emp.id, is_active=True)
    _make_record(db_session, emp.id, admin.id, date.today() - timedelta(days=1))
    db_session.commit()
    uid, eid = user.id, emp.id

    result = run_offboarding_revoke_due_once()
    assert result["revoked"] == 1

    db_session.expire_all()
    assert db_session.get(_User, uid).is_active is False
    assert db_session.get(EmployeeOffboardingRecord, eid).user_revoked_at is not None
