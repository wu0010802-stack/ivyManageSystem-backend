"""
test_pos_operator_activity.py — 驗證 POS 操作員活動稽核 dashboard 端點（spec H1）。

對齊 spec: docs/superpowers/specs/2026-05-07-pos-operator-activity-design.md
"""

import os
import sys
from datetime import date, datetime, timedelta

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
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission

from tests.test_activity_pos import _create_admin, _login

APPROVE_PERMS = (
    Permission.ACTIVITY_READ
    | Permission.ACTIVITY_WRITE
    | Permission.ACTIVITY_PAYMENT_APPROVE
)
READ_ONLY = Permission.ACTIVITY_READ


@pytest.fixture
def operator_client(tmp_path):
    db_path = tmp_path / "operator_activity.sqlite"
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


def _make_reg(s, student_name="X"):
    """建立含一門課程的 registration（避開 _setup_reg 的 supply 依賴，較簡潔）。"""
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
        student_name=student_name,
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


def _add_record(
    s,
    *,
    reg_id,
    operator,
    type_="payment",
    amount=500,
    day=None,
    voided=False,
):
    """直接寫 ActivityPaymentRecord（繞過 API 以便控制 operator/day/voided）。"""
    if day is None:
        day = date.today()
    rec = ActivityPaymentRecord(
        registration_id=reg_id,
        type=type_,
        amount=amount,
        payment_date=day,
        payment_method="現金",
        operator=operator,
        notes="",
        created_at=datetime.now(),
    )
    if voided:
        rec.voided_at = datetime.now()
        rec.voided_by = "tester"
        rec.void_reason = "test void"
    s.add(rec)
    s.flush()
    return rec


# ── Test 1-7 ─────────────────────────────────────────────────────────


def test_returns_operators_with_counts(operator_client):
    """收 2 筆（A）+ 退 1 筆（B），endpoint 回 2 個 operator，payment/refund 數正確。"""
    client, sf = operator_client
    with sf() as s:
        _create_admin(s, permissions=APPROVE_PERMS)
        reg = _make_reg(s, student_name="王同學")
        _add_record(s, reg_id=reg.id, operator="alice", type_="payment", amount=500)
        _add_record(s, reg_id=reg.id, operator="alice", type_="payment", amount=300)
        _add_record(s, reg_id=reg.id, operator="bob", type_="refund", amount=100)
        s.commit()

    assert _login(client).status_code == 200
    res = client.get("/api/activity/audit/operator-activity?days=30")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 2
    by_op = {r["operator"]: r for r in body["operators"]}
    assert by_op["alice"]["payment_count"] == 2
    assert by_op["alice"]["refund_count"] == 0
    assert by_op["alice"]["total_count"] == 2
    assert by_op["bob"]["payment_count"] == 0
    assert by_op["bob"]["refund_count"] == 1


def test_orders_by_total_count_desc(operator_client):
    """A 收 5 筆、B 收 1 筆 → A 排第一。"""
    client, sf = operator_client
    with sf() as s:
        _create_admin(s, permissions=APPROVE_PERMS)
        reg = _make_reg(s, student_name="李同學")
        for _ in range(5):
            _add_record(s, reg_id=reg.id, operator="heavy", type_="payment", amount=100)
        _add_record(s, reg_id=reg.id, operator="light", type_="payment", amount=100)
        s.commit()

    assert _login(client).status_code == 200
    res = client.get("/api/activity/audit/operator-activity?days=30")
    operators = res.json()["operators"]
    assert operators[0]["operator"] == "heavy"
    assert operators[1]["operator"] == "light"


def test_excludes_voided_records(operator_client):
    """voided 紀錄不計入。"""
    client, sf = operator_client
    with sf() as s:
        _create_admin(s, permissions=APPROVE_PERMS)
        reg = _make_reg(s, student_name="陳同學")
        _add_record(s, reg_id=reg.id, operator="alice", type_="payment", amount=200)
        _add_record(s, reg_id=reg.id, operator="alice", type_="payment", amount=200)
        _add_record(
            s, reg_id=reg.id, operator="alice", type_="payment", amount=200, voided=True
        )
        s.commit()

    assert _login(client).status_code == 200
    res = client.get("/api/activity/audit/operator-activity?days=30")
    body = res.json()
    assert body["operators"][0]["payment_count"] == 2  # voided 那筆排除


def test_user_field_null_when_no_account(operator_client):
    """operator 沒對應 User → response 該筆 user=None。"""
    client, sf = operator_client
    with sf() as s:
        _create_admin(s, permissions=APPROVE_PERMS)
        reg = _make_reg(s, student_name="林同學")
        # operator='ghost_account' 沒有對應 User row
        _add_record(s, reg_id=reg.id, operator="ghost_account", type_="payment")
        s.commit()

    assert _login(client).status_code == 200
    res = client.get("/api/activity/audit/operator-activity?days=30")
    body = res.json()
    ghost = next(r for r in body["operators"] if r["operator"] == "ghost_account")
    assert ghost["user"] is None


def test_user_field_includes_display_name_role(operator_client):
    """operator 對應 User → response 含 display_name / role / employee_id / is_active。"""
    client, sf = operator_client
    with sf() as s:
        # 既有 admin（pos_admin）
        _create_admin(s, permissions=APPROVE_PERMS)
        # 新增帶 display_name 的個人帳號
        s.add(
            User(
                username="alice",
                password_hash=hash_password("Pw123456"),
                role="staff",
                display_name="愛麗絲老師",
                permissions=APPROVE_PERMS,
                is_active=True,
            )
        )
        reg = _make_reg(s, student_name="蔡同學")
        _add_record(s, reg_id=reg.id, operator="alice", type_="payment")
        s.commit()

    assert _login(client).status_code == 200
    res = client.get("/api/activity/audit/operator-activity?days=30")
    body = res.json()
    alice = next(r for r in body["operators"] if r["operator"] == "alice")
    assert alice["user"] is not None
    assert alice["user"]["display_name"] == "愛麗絲老師"
    assert alice["user"]["role"] == "staff"
    assert alice["user"]["is_active"] is True


def test_permission_guard_403_without_approve(operator_client):
    """一般 ACTIVITY_READ user 不可看 → 403。"""
    client, sf = operator_client
    with sf() as s:
        s.add(
            User(
                username="reader",
                password_hash=hash_password("Pw123456"),
                role="staff",
                permissions=READ_ONLY,
                is_active=True,
            )
        )
        s.commit()

    assert _login(client, "reader", "Pw123456").status_code == 200
    res = client.get("/api/activity/audit/operator-activity?days=30")
    assert res.status_code == 403, res.text


def test_days_query_limits_window(operator_client):
    """早於 cutoff 的紀錄不計入。"""
    client, sf = operator_client
    with sf() as s:
        _create_admin(s, permissions=APPROVE_PERMS)
        reg = _make_reg(s, student_name="周同學")
        # 60 天前的紀錄
        old_day = date.today() - timedelta(days=60)
        _add_record(s, reg_id=reg.id, operator="alice", type_="payment", day=old_day)
        # 5 天前的紀錄
        recent_day = date.today() - timedelta(days=5)
        _add_record(s, reg_id=reg.id, operator="alice", type_="payment", day=recent_day)
        s.commit()

    assert _login(client).status_code == 200
    # 30 天視窗：只看到 5 天前那筆
    res = client.get("/api/activity/audit/operator-activity?days=30")
    body = res.json()
    alice = next(r for r in body["operators"] if r["operator"] == "alice")
    assert alice["payment_count"] == 1
    # 90 天視窗：兩筆都在
    res = client.get("/api/activity/audit/operator-activity?days=90")
    body = res.json()
    alice = next(r for r in body["operators"] if r["operator"] == "alice")
    assert alice["payment_count"] == 2
