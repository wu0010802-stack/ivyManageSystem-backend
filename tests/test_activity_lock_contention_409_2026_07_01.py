"""tests/test_activity_lock_contention_409_2026_07_01.py

A-2（2026-07-01 才藝 bug hunt）：一批 course-first 寫入端點鎖爭用/死鎖被翻成 500。

這些端點對熱門 ActivityCourse / reg 列下 with_for_update 並（部分）呼叫
_auto_promote_first_waitlist，但 except 鏈只有 `except Exception → raise_safe_500`，
故 PostgreSQL 鎖爭用（55P03）或死鎖（40P01）都被翻成 HTTP 500，而非公開端與
pending-workflow 端一致的「可重試 409」。

修法：於 `except Exception` 前補 `except OperationalError → raise_lock_contention_or_500`
（40P01/55P03 → 409，其餘 → 500），與 public.py / registrations_pending 既有守衛對齊。

本測以 monkeypatch 讓 handler try 內某呼叫拋模擬的 OperationalError(40P01)，斷言端點
回 409（修前為 500）。代表性覆蓋兩檔：
- registrations_items.add_registration_course（該檔原本連 OperationalError 都沒 import）
- registrations.delete_registration
（同檔的 add_registration_supply / remove_registration_supply / withdraw_course /
promote_waitlist / sweep_expired_waitlist_promotions 與 registrations_pending.reject_registration
套用同一機械式 clause。）
"""

import os
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.activity.registrations_items as items_mod
import models.base as base_module
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    RegistrationCourse,
    User,
)
from services.activity_service import activity_service
from utils.auth import hash_password


def _lock_op_error(*_a, **_k):
    """模擬 SQLAlchemy OperationalError：.orig.pgcode=40P01（死鎖）。"""
    raise OperationalError("SELECT 1", {}, types.SimpleNamespace(pgcode="40P01"))


@pytest.fixture
def lock_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'lock-409.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)
    # raise_server_exceptions=False：讓「未轉譯的 OperationalError」以 500 response 呈現
    # （而非拋回測試），使修前 RED=500、修後 GREEN=409 的斷言穩定。
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _login(client):
    return client.post(
        "/api/auth/login", json={"username": "lock_admin", "password": "TempPass123"}
    )


def _seed_admin(session):
    session.add(
        User(
            username="lock_admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
            is_active=True,
        )
    )
    session.flush()


def _seed_reg_course(session):
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    course = ActivityCourse(
        name="圍棋",
        price=1000,
        capacity=30,
        allow_waitlist=True,
        is_active=True,
        school_year=sy,
        semester=sem,
    )
    session.add(course)
    session.flush()
    reg = ActivityRegistration(
        student_name="王小明",
        birthday="2020-01-01",
        class_name="大班",
        parent_phone="0912345678",
        paid_amount=0,
        is_paid=False,
        is_active=True,
        school_year=sy,
        semester=sem,
    )
    session.add(reg)
    session.flush()
    session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="enrolled",
            price_snapshot=1000,
        )
    )
    session.flush()
    return reg, course


def test_add_registration_course_lock_contention_returns_409(lock_client, monkeypatch):
    client, sf = lock_client
    with sf() as s:
        _seed_admin(s)
        reg, _course = _seed_reg_course(s)
        from utils.academic import resolve_current_academic_term

        sy, sem = resolve_current_academic_term()
        extra = ActivityCourse(
            name="珠心算",
            price=1000,
            capacity=30,
            allow_waitlist=True,
            is_active=True,
            school_year=sy,
            semester=sem,
        )
        s.add(extra)
        s.commit()
        reg_id = reg.id
        extra_id = extra.id

    assert _login(client).status_code == 200
    # handler try 內鎖 reg 行時拋死鎖 → 應轉 409（修前落 except Exception → 500）
    monkeypatch.setattr(items_mod, "_lock_registration", _lock_op_error)

    res = client.post(
        f"/api/activity/registrations/{reg_id}/courses",
        json={"course_id": extra_id},
    )
    assert res.status_code == 409, res.text


def test_delete_registration_lock_contention_returns_409(lock_client, monkeypatch):
    client, sf = lock_client
    with sf() as s:
        _seed_admin(s)
        reg, _course = _seed_reg_course(s)
        s.commit()
        reg_id = reg.id

    assert _login(client).status_code == 200
    # service.delete_registration 於刪除/遞補時撞死鎖 → 應轉 409
    monkeypatch.setattr(activity_service, "delete_registration", _lock_op_error)

    res = client.request("DELETE", f"/api/activity/registrations/{reg_id}")
    assert res.status_code == 409, res.text
