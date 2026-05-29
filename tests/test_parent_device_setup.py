"""家長端無 LINE 裝置登入（設定碼）測試。"""

from __future__ import annotations

import os
import sys
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import Base, Guardian, ParentDeviceSetupCode, Student, User
from utils.taipei_time import now_taipei_naive


def test_model_creates_with_expected_columns():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    stu = Student(student_id="S1", name="小明", is_active=True)
    s.add(stu)
    s.flush()
    g = Guardian(student_id=stu.id, name="王大明", is_primary=True)
    s.add(g)
    s.flush()
    u = User(username="staff1", password_hash="x", role="teacher", token_version=0)
    s.add(u)
    s.flush()
    code = ParentDeviceSetupCode(
        guardian_id=g.id,
        code_hash="a" * 64,
        expires_at=now_taipei_naive() + timedelta(hours=24),
        created_by=u.id,
    )
    s.add(code)
    s.commit()
    row = s.query(ParentDeviceSetupCode).one()
    assert row.guardian_id == g.id
    assert row.used_at is None
    assert row.used_by_user_id is None
    s.close()
    engine.dispose()


# ── Task 3 helper tests ──────────────────────────────────────────────────────


def _mk_engine_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


def _seed_guardian(session, *, with_user=False):
    stu = Student(student_id="S1", name="小明", is_active=True)
    session.add(stu)
    session.flush()
    g = Guardian(student_id=stu.id, name="王大明", is_primary=True)
    session.add(g)
    session.flush()
    return g


class TestDeviceSetupHelpers:
    def test_claim_atomic_single_use(self):
        from api.parent_portal import auth as pauth

        engine, s = _mk_engine_session()
        g = _seed_guardian(s)
        staff = User(
            username="staff", password_hash="x", role="teacher", token_version=0
        )
        s.add(staff)
        s.flush()
        s.add(
            ParentDeviceSetupCode(
                guardian_id=g.id,
                code_hash=pauth._hash_code("CODE123"),
                expires_at=now_taipei_naive() + timedelta(hours=24),
                created_by=staff.id,
            )
        )
        s.commit()
        first = pauth._claim_device_setup_code_atomic(s, pauth._hash_code("CODE123"))
        assert first is not None and first.used_at is not None
        second = pauth._claim_device_setup_code_atomic(s, pauth._hash_code("CODE123"))
        assert second is None
        s.close()
        engine.dispose()

    def test_create_parent_user_for_device_no_line(self):
        from api.parent_portal import auth as pauth

        engine, s = _mk_engine_session()
        g = _seed_guardian(s)
        s.commit()
        u = pauth._create_parent_user_for_device(s, g)
        s.commit()
        assert u.role == "parent"
        assert u.line_user_id is None
        assert u.username == f"parent_device_{g.id}"
        assert u.display_name == "王大明"
        s.close()
        engine.dispose()


# ── Task 4 endpoint tests ────────────────────────────────────────────────────

import hashlib
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

import models.base as base_module
from api.auth import router as auth_router, _ip_attempts, _account_failures
from api.parent_portal import (
    admin_router as parent_admin_router,
    parent_router as parent_portal_router,
    init_parent_line_service,
)
from api.parent_portal.auth import _bind_failures
from utils.exception_handlers import register_exception_handlers


class _FakeLine:
    def is_configured(self):
        return True

    def verify_id_token(self, t):
        raise AssertionError("device-setup 不應呼叫 LINE")


@pytest.fixture
def pclient(tmp_path):
    db_path = tmp_path / "dev-setup.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine, base_module._SessionFactory = engine, sf
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    _bind_failures.clear()
    init_parent_line_service(_FakeLine())
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(parent_portal_router)
    app.include_router(parent_admin_router)
    with TestClient(app) as c:
        yield c, sf
    base_module._engine, base_module._SessionFactory = old_e, old_sf
    engine.dispose()


def _seed_code(sf, *, used=False, expired=False):
    from api.parent_portal import auth as pauth

    s = sf()
    stu = Student(student_id="S9", name="小華", is_active=True)
    s.add(stu)
    s.flush()
    g = Guardian(student_id=stu.id, name="陳媽媽", is_primary=True)
    s.add(g)
    s.flush()
    staff = User(username="adm", password_hash="x", role="admin", token_version=0)
    s.add(staff)
    s.flush()
    exp = now_taipei_naive() + (timedelta(hours=-1) if expired else timedelta(hours=24))
    code = ParentDeviceSetupCode(
        guardian_id=g.id,
        code_hash=pauth._hash_code("DEVCODE0001"),
        expires_at=exp,
        created_by=staff.id,
        used_at=(now_taipei_naive() if used else None),
    )
    s.add(code)
    s.commit()
    gid = g.id
    s.close()
    return gid


class TestDeviceSetupEndpoint:
    def test_success_creates_user_and_session(self, pclient):
        c, sf = pclient
        gid = _seed_code(sf)
        r = c.post("/api/parent/auth/device-setup", json={"code": "DEVCODE0001"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["user"]["role"] == "parent"
        assert "parent_refresh_token" in r.cookies
        s = sf()
        g = s.query(Guardian).filter(Guardian.id == gid).first()
        assert g.user_id is not None
        u = s.query(User).filter(User.id == g.user_id).first()
        assert u.line_user_id is None and u.role == "parent"
        s.close()

    def test_expired_code_generic_error(self, pclient):
        c, sf = pclient
        _seed_code(sf, expired=True)
        r = c.post("/api/parent/auth/device-setup", json={"code": "DEVCODE0001"})
        assert r.status_code == 400
        assert "無效或已過期" in r.text

    def test_used_code_generic_error(self, pclient):
        c, sf = pclient
        _seed_code(sf, used=True)
        r = c.post("/api/parent/auth/device-setup", json={"code": "DEVCODE0001"})
        assert r.status_code == 400
        assert "無效或已過期" in r.text

    def test_unknown_code_generic_error(self, pclient):
        c, sf = pclient
        _seed_code(sf)
        r = c.post("/api/parent/auth/device-setup", json={"code": "WRONGCODE99"})
        assert r.status_code == 400
        assert "無效或已過期" in r.text

    def test_second_device_reuses_user_keeps_both(self, pclient):
        c, sf = pclient
        gid = _seed_code(sf)
        r1 = c.post("/api/parent/auth/device-setup", json={"code": "DEVCODE0001"})
        assert r1.status_code == 200
        from api.parent_portal import auth as pauth

        s = sf()
        staff = s.query(User).filter(User.role == "admin").first()
        s.add(
            ParentDeviceSetupCode(
                guardian_id=gid,
                code_hash=pauth._hash_code("DEVCODE0002"),
                expires_at=now_taipei_naive() + timedelta(hours=24),
                created_by=staff.id,
            )
        )
        s.commit()
        s.close()
        r2 = c.post("/api/parent/auth/device-setup", json={"code": "DEVCODE0002"})
        assert r2.status_code == 200
        s = sf()
        g = s.query(Guardian).filter(Guardian.id == gid).first()
        from models.database import ParentRefreshToken

        n_users = s.query(User).filter(User.username == f"parent_device_{gid}").count()
        n_tokens = (
            s.query(ParentRefreshToken)
            .filter(
                ParentRefreshToken.user_id == g.user_id,
                ParentRefreshToken.revoked_at.is_(None),
            )
            .count()
        )
        assert n_users == 1
        assert n_tokens == 2
        s.close()
