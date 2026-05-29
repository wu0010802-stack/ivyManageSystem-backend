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
