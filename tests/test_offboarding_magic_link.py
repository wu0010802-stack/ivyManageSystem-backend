"""驗證 magic_link service：token 產生 / hash 比對 / 撤銷 / active 判斷。"""

import hashlib
import os
import sys
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, User
from models.offboarding import EmployeeOffboardingRecord
from utils.auth import hash_password

from services.offboarding.magic_link import (
    TOKEN_TTL_DAYS,
    MAX_DOWNLOADS,
    MagicLinkError,
    generate_token,
    hash_token,
    is_active,
    record_download,
    revoke_token,
    verify_token,
)

_counter = 0


@pytest.fixture
def db_session(tmp_path):
    """SQLite test session（對齊 test_offboarding_orchestrator.py pattern）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "offboarding_magic_link.sqlite"
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
    def _factory(*, name="測試員工", id_number=None) -> Employee:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"ML{_counter:04d}",
            name=name,
            hire_date=date(2020, 1, 1),
            is_active=True,
            base_salary=50000,
            id_number=id_number or f"A{_counter:08d}",
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
            username=f"mluser{_counter}",
            password_hash=hash_password("Passw0rd!"),
            role=role,
            is_active=True,
            token_version=0,
        )
        db_session.add(u)
        db_session.flush()
        return u

    return _factory


def _make_record(db_session, employee_id, user_id):
    rec = EmployeeOffboardingRecord(
        employee_id=employee_id,
        resign_date=date(2026, 6, 15),
        opened_at=datetime.now(),
        opened_by_user_id=user_id,
    )
    db_session.add(rec)
    db_session.flush()
    return rec


def test_constants():
    """TTL 30 天 / 下載 3 次上限符合 spec §8。"""
    assert TOKEN_TTL_DAYS == 30
    assert MAX_DOWNLOADS == 3


def test_generate_token_returns_256_bit_url_safe_random(
    db_session,
    employee_factory,
    user_factory,
):
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)

    token = generate_token(db_session, record)

    # secrets.token_urlsafe(32) → 約 43 字元 base64url
    assert len(token) >= 40
    assert all(c.isalnum() or c in "-_" for c in token)

    # DB 存的是 hash 非明文
    db_session.refresh(record)
    assert record.magic_link_token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert record.magic_link_expires_at > datetime.now() + timedelta(days=29)
    assert record.magic_link_revoked_at is None
    assert record.magic_link_download_count == 0


def test_generate_token_overwrites_previous(
    db_session,
    employee_factory,
    user_factory,
):
    """重發 token = 舊 hash 失效，count 歸 0。"""
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)

    t1 = generate_token(db_session, record)
    # 模擬已下載 1 次
    record.magic_link_download_count = 1
    db_session.flush()

    t2 = generate_token(db_session, record)
    db_session.refresh(record)

    assert t1 != t2
    assert record.magic_link_token_hash == hashlib.sha256(t2.encode()).hexdigest()
    assert record.magic_link_download_count == 0  # 歸 0


def test_verify_token_returns_record_for_valid(
    db_session,
    employee_factory,
    user_factory,
):
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)
    token = generate_token(db_session, record)
    db_session.commit()

    found = verify_token(db_session, token)
    assert found is not None
    assert found.employee_id == emp.id


def test_verify_token_returns_none_for_unknown(db_session):
    """未知 token → None（不暴露差異避免 enumeration）。"""
    assert verify_token(db_session, "nonexistent-token-string") is None


def test_verify_token_returns_none_for_revoked(
    db_session,
    employee_factory,
    user_factory,
):
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)
    token = generate_token(db_session, record)
    revoke_token(db_session, record)
    db_session.commit()

    assert verify_token(db_session, token) is None


def test_verify_token_returns_none_for_expired(
    db_session,
    employee_factory,
    user_factory,
):
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)
    token = generate_token(db_session, record)
    # 強制過期
    record.magic_link_expires_at = datetime.now() - timedelta(days=1)
    db_session.commit()

    assert verify_token(db_session, token) is None


def test_verify_token_returns_none_when_max_downloads_reached(
    db_session,
    employee_factory,
    user_factory,
):
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)
    token = generate_token(db_session, record)
    record.magic_link_download_count = 3
    db_session.commit()

    assert verify_token(db_session, token) is None


def test_is_active_logic(db_session, employee_factory, user_factory):
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)

    # 無 token → inactive
    assert is_active(record) is False

    generate_token(db_session, record)
    db_session.refresh(record)
    assert is_active(record) is True

    revoke_token(db_session, record)
    db_session.refresh(record)
    assert is_active(record) is False
