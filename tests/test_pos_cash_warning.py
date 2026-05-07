"""
test_pos_cash_warning.py — 驗證 daily-summary 現金累積警報（spec H7）。

業務語意：當日預期現金 ≥ NT$30,000 → cash_warning=True，前端顯示
「請存銀行」橘色提示，避免抽屜累積大量現金被竊損失。
"""

import os
import sys
from datetime import date, datetime

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
    ActivityPaymentRecord,
    ActivityRegistration,
    Base,
    RegistrationCourse,
)
from utils.permissions import Permission

from tests.test_activity_pos import _create_admin, _login

READ_PERMS = Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE


@pytest.fixture
def cash_client(tmp_path):
    db_path = tmp_path / "cash_warning.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
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


def _make_reg(s, name="X"):
    course = s.query(ActivityCourse).filter(ActivityCourse.name == "美術").first()
    if not course:
        course = ActivityCourse(
            name="美術",
            price=1000,
            capacity=30,
            allow_waitlist=True,
            school_year=114,
            semester=1,
        )
        s.add(course)
        s.flush()
    reg = ActivityRegistration(
        student_name=name,
        birthday="2020-01-01",
        class_name="大班",
        paid_amount=0,
        is_paid=False,
        is_active=True,
        school_year=114,
        semester=1,
    )
    s.add(reg)
    s.flush()
    s.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="enrolled",
            price_snapshot=1000,
        )
    )
    s.flush()
    return reg


def _add_cash_payment(s, reg_id, amount, day=None):
    s.add(
        ActivityPaymentRecord(
            registration_id=reg_id,
            type="payment",
            amount=amount,
            payment_date=day or date.today(),
            payment_method="現金",
            operator="pos_admin",
            notes="",
            created_at=datetime.now(),
        )
    )


def test_no_warning_below_threshold(cash_client):
    """預期現金 NT$10,000 < NT$30,000 → cash_warning=False。"""
    client, sf = cash_client
    today = date.today()
    with sf() as s:
        _create_admin(s, permissions=READ_PERMS)
        reg = _make_reg(s, "甲")
        _add_cash_payment(s, reg.id, 10000, day=today)
        s.commit()

    assert _login(client).status_code == 200
    res = client.get("/api/activity/pos/daily-summary")
    assert res.status_code == 200
    body = res.json()
    assert body["cash_in_drawer"] == 10000
    assert body["cash_warning"] is False
    assert body["cash_warning_threshold"] == 30000


def test_warning_at_threshold(cash_client):
    """預期現金 NT$30,000 = 門檻 → cash_warning=True（>=）。"""
    client, sf = cash_client
    today = date.today()
    with sf() as s:
        _create_admin(s, permissions=READ_PERMS)
        reg = _make_reg(s, "乙")
        _add_cash_payment(s, reg.id, 30000, day=today)
        s.commit()

    assert _login(client).status_code == 200
    res = client.get("/api/activity/pos/daily-summary")
    body = res.json()
    assert body["cash_in_drawer"] == 30000
    assert body["cash_warning"] is True


def test_warning_above_threshold(cash_client):
    """累積多筆，總和 NT$50,000 → cash_warning=True。"""
    client, sf = cash_client
    today = date.today()
    with sf() as s:
        _create_admin(s, permissions=READ_PERMS)
        reg = _make_reg(s, "丙")
        for amt in (10000, 20000, 20000):
            _add_cash_payment(s, reg.id, amt, day=today)
        s.commit()

    assert _login(client).status_code == 200
    res = client.get("/api/activity/pos/daily-summary")
    body = res.json()
    assert body["cash_in_drawer"] == 50000
    assert body["cash_warning"] is True


def test_refund_offsets_cash_in_drawer(cash_client):
    """收 NT$40,000 + 退 NT$15,000 → 抽屜淨額 NT$25,000 < 30,000 → 無警示。

    Why: by_method_net 已經 net 化（payment - refund），抽屜實際只有差額。
    """
    client, sf = cash_client
    today = date.today()
    with sf() as s:
        _create_admin(s, permissions=READ_PERMS)
        reg = _make_reg(s, "丁")
        _add_cash_payment(s, reg.id, 40000, day=today)
        # 退費 15000
        s.add(
            ActivityPaymentRecord(
                registration_id=reg.id,
                type="refund",
                amount=15000,
                payment_date=today,
                payment_method="現金",
                operator="pos_admin",
                notes="",
                created_at=datetime.now(),
            )
        )
        s.commit()

    assert _login(client).status_code == 200
    res = client.get("/api/activity/pos/daily-summary")
    body = res.json()
    assert body["cash_in_drawer"] == 25000
    assert body["cash_warning"] is False
