"""tests/test_pos_semester_reconciliation.py — 學期對帳總表端點測試。

涵蓋四態簽核判斷與 approval_status 篩選。
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
    ActivityPosDailyClose,
    ActivityRegistration,
    ActivitySupply,
    Base,
    RegistrationCourse,
    RegistrationSupply,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def pos_client(tmp_path):
    db_path = tmp_path / "pos_semester.sqlite"
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


def _create_admin(
    session,
    username: str = "pos_admin",
    password: str = "TempPass123",
    permissions: int = Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=permissions,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username="pos_admin", password="TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _setup_reg(
    session,
    *,
    student_name: str,
    class_name: str = "玫瑰",
    course_price: int = 2000,
    paid_amount: int = 0,
    is_paid: bool = False,
    course_name: str = "美術",
) -> ActivityRegistration:
    """建立本學期一筆報名（1 門課，含用品）。total = course_price。"""
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    course = (
        session.query(ActivityCourse)
        .filter(
            ActivityCourse.name == course_name,
            ActivityCourse.school_year == sy,
            ActivityCourse.semester == sem,
        )
        .first()
    )
    if not course:
        course = ActivityCourse(
            name=course_name,
            price=course_price,
            capacity=30,
            allow_waitlist=True,
            school_year=sy,
            semester=sem,
        )
        session.add(course)
        session.flush()

    reg = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name=class_name,
        paid_amount=paid_amount,
        is_paid=is_paid,
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
            price_snapshot=course_price,
        )
    )
    session.flush()
    return reg


def _add_payment(
    session,
    *,
    reg_id: int,
    amount: int,
    payment_date: date,
    type_: str = "payment",
    method: str = "現金",
    notes: str = "[POS-TEST]",
):
    rec = ActivityPaymentRecord(
        registration_id=reg_id,
        type=type_,
        amount=amount,
        payment_date=payment_date,
        payment_method=method,
        notes=notes,
        operator="pos_admin",
    )
    session.add(rec)
    session.flush()
    return rec


def _mark_closed(session, close_date: date):
    row = ActivityPosDailyClose(
        close_date=close_date,
        approver_username="pos_admin",
        approved_at=datetime.now(),
        payment_total=0,
        refund_total=0,
        net_total=0,
        transaction_count=0,
        by_method_json="{}",
    )
    session.add(row)
    session.flush()


def _get(client, **params):
    return client.get("/api/activity/pos/semester-reconciliation", params=params)


def _find_item(data, reg_id):
    for it in data["items"]:
        if it["id"] == reg_id:
            return it
    raise AssertionError(f"reg {reg_id} not in items")


# ── 測試 ─────────────────────────────────────────────────────────────


def test_fully_approved(pos_client):
    client, sf = pos_client
    d1 = date.today() - timedelta(days=3)
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="全簽核", paid_amount=2000, is_paid=True)
        _add_payment(s, reg_id=reg.id, amount=2000, payment_date=d1)
        _mark_closed(s, d1)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    assert res.status_code == 200, res.text
    data = res.json()
    it = _find_item(data, rid)
    assert it["approval_status"] == "fully_approved"
    assert it["approved_paid_amount"] == 2000
    assert it["pending_paid_amount"] == 0


def test_partially_approved(pos_client):
    client, sf = pos_client
    d1 = date.today() - timedelta(days=3)  # 已簽核
    d2 = date.today() - timedelta(days=1)  # 未簽核
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="半簽核", paid_amount=1500, is_paid=False)
        _add_payment(s, reg_id=reg.id, amount=1000, payment_date=d1)
        _add_payment(s, reg_id=reg.id, amount=500, payment_date=d2)
        _mark_closed(s, d1)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    assert res.status_code == 200
    it = _find_item(res.json(), rid)
    assert it["approval_status"] == "partially_approved"
    assert it["approved_paid_amount"] == 1000
    assert it["pending_paid_amount"] == 500


def test_pending_approval(pos_client):
    client, sf = pos_client
    d1 = date.today()
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="待簽核", paid_amount=800, is_paid=False)
        _add_payment(s, reg_id=reg.id, amount=800, payment_date=d1)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    it = _find_item(res.json(), rid)
    assert it["approval_status"] == "pending_approval"
    assert it["approved_paid_amount"] == 0
    assert it["pending_paid_amount"] == 800


def test_no_payment(pos_client):
    client, sf = pos_client
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="未繳費", paid_amount=0)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    it = _find_item(res.json(), rid)
    assert it["approval_status"] == "no_payment"
    assert it["paid_amount"] == 0
    assert it["owed"] == 2000


def test_filter_by_approval_status(pos_client):
    client, sf = pos_client
    d1 = date.today() - timedelta(days=3)
    d2 = date.today()
    with sf() as s:
        _create_admin(s)
        r_full = _setup_reg(s, student_name="全簽核", paid_amount=2000, is_paid=True)
        _add_payment(s, reg_id=r_full.id, amount=2000, payment_date=d1)
        _mark_closed(s, d1)

        r_pend = _setup_reg(
            s, student_name="待簽核", paid_amount=2000, is_paid=True, course_name="勞作"
        )
        _add_payment(s, reg_id=r_pend.id, amount=2000, payment_date=d2)

        r_none = _setup_reg(s, student_name="未繳費", paid_amount=0, course_name="圍棋")
        s.commit()
        rid_full, rid_pend, rid_none = r_full.id, r_pend.id, r_none.id

    assert _login(client).status_code == 200

    # 不過濾：3 筆都在
    res_all = _get(client)
    ids_all = {it["id"] for it in res_all.json()["items"]}
    assert {rid_full, rid_pend, rid_none} <= ids_all

    # 過濾 pending_approval：只 r_pend
    res = _get(client, approval_status="pending_approval")
    assert res.status_code == 200
    items = res.json()["items"]
    assert [it["id"] for it in items] == [rid_pend]

    # 過濾 no_payment：只 r_none
    res = _get(client, approval_status="no_payment")
    assert [it["id"] for it in res.json()["items"]] == [rid_none]

    # totals 反映過濾後結果
    res = _get(client, approval_status="fully_approved")
    totals = res.json()["totals"]
    assert totals["registration_count"] == 1
    assert totals["approved_paid_amount"] == 2000
    assert totals["pending_paid_amount"] == 0


def test_offline_paid_no_records(pos_client):
    """reg 有 paid_amount 但完全沒 payment_record（歷史匯入資料）。"""
    client, sf = pos_client
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="歷史匯入", paid_amount=1800, is_paid=False)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    assert res.status_code == 200
    data = res.json()
    it = _find_item(data, rid)
    # 無 POS 紀錄但有 paid_amount → 歸類 pending_approval（尚未透過日結覆蓋）
    assert it["approval_status"] == "pending_approval"
    assert it["approved_paid_amount"] == 0
    assert it["pending_paid_amount"] == 0
    assert it["offline_paid_amount"] == 1800
    # totals 也要累計
    assert data["totals"]["offline_paid_amount"] >= 1800


def test_offline_paid_partial_mix(pos_client):
    """reg.paid_amount 比 POS 紀錄加總大 → 差額視為 offline_paid。"""
    client, sf = pos_client
    d1 = date.today() - timedelta(days=2)
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="混合匯入", paid_amount=2000, is_paid=False)
        _add_payment(s, reg_id=reg.id, amount=500, payment_date=d1)
        _mark_closed(s, d1)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    it = _find_item(res.json(), rid)
    # 500 已簽核、其餘 1500 沒 POS 紀錄
    assert it["approved_paid_amount"] == 500
    assert it["pending_paid_amount"] == 0
    assert it["offline_paid_amount"] == 1500


def test_invalid_approval_status_rejected(pos_client):
    client, _sf = pos_client
    with pos_client[1]() as s:
        _create_admin(s)
        s.commit()

    assert _login(client).status_code == 200
    res = _get(client, approval_status="not_a_valid_state")
    assert res.status_code == 400
