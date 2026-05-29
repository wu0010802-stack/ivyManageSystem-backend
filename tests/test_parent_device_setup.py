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
