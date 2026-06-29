"""P2-7 回歸（2026-06-23 深度 audit，業主裁示）：POS checkout 放寬允許超收，
與單筆繳費端點 add_registration_payment 口徑一致（overpaid 為系統支援的付款四態之一）。

原本 POS checkout 對「已繳 + 本次 > 應繳」回 400 擋超收，但單筆端點允許超收，
兩條繳費路徑方向相反。業主裁示放寬 POS。空報名（total<=0）守衛保留。
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,  # noqa: F401 metadata
    RegistrationCourse,
    User,
)
from utils.auth import hash_password


@pytest.fixture
def pos_client(tmp_path):
    db_path = tmp_path / "pos_overpay.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)
    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed(session, *, course_price=2000):
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    session.add(
        User(
            username="pos_admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"],
            is_active=True,
        )
    )
    course = ActivityCourse(
        name="美術",
        price=course_price,
        capacity=30,
        school_year=sy,
        semester=sem,
        is_active=True,
    )
    session.add(course)
    session.flush()
    reg = ActivityRegistration(
        student_name="王小明",
        birthday="2020-01-01",
        class_name="大班",
        paid_amount=0,
        is_active=True,
        match_status="matched",
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
            price_snapshot=course_price,
        )
    )
    session.commit()
    return reg.id


def _login(client):
    return client.post(
        "/api/auth/login", json={"username": "pos_admin", "password": "TempPass123"}
    )


def test_pos_checkout_allows_overpay(pos_client):
    """應繳 2000、本次收 2500（超收）→ 放行（201），paid_amount=2500、狀態 overpaid。"""
    client, sf = pos_client
    with sf() as s:
        reg_id = _seed(s, course_price=2000)
    assert _login(client).status_code == 200

    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": reg_id, "amount": 2500}],
            "payment_method": "現金",
            "payment_date": date.today().isoformat(),
            "idempotency_key": "OVERPAY-CHECKOUT-001",
        },
    )
    assert res.status_code == 201, res.text
    with sf() as s:
        reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
        assert reg.paid_amount == 2500


def test_pos_checkout_still_blocks_empty_registration(pos_client):
    """空報名（無應繳）守衛保留：total<=0 仍不得收款（400）。"""
    client, sf = pos_client
    from utils.academic import resolve_current_academic_term

    with sf() as s:
        _seed(s, course_price=2000)  # 建 admin
        sy, sem = resolve_current_academic_term()
        empty = ActivityRegistration(
            student_name="無課學生",
            birthday="2020-02-02",
            class_name="大班",
            paid_amount=0,
            is_active=True,
            match_status="matched",
            school_year=sy,
            semester=sem,
        )
        s.add(empty)
        s.commit()
        empty_id = empty.id
    assert _login(client).status_code == 200

    res = client.post(
        "/api/activity/pos/checkout",
        json={
            "items": [{"registration_id": empty_id, "amount": 500}],
            "payment_method": "現金",
            "payment_date": date.today().isoformat(),
            "idempotency_key": "OVERPAY-EMPTY-001",
        },
    )
    assert res.status_code == 400, res.text
