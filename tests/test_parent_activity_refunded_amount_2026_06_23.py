"""家長端 my-registrations 回傳 refunded_amount（refunded 維度，2026-06-23）。

檢視 finding：退費後 paid_amount 往下扣，「收5000又退5000→paid=0」與「從沒收過錢→
paid=0」在 payment_status 上無法區分。新增 refunded_amount（type='refund' 未作廢之和）
讓家長端可分辨並顯示「已退費」。
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
from api.parent_portal import parent_router as parent_portal_router
from models.activity import (
    ActivityPaymentRecord,
    ActivityRegistration,
)
from models.database import Base, Classroom, Guardian, Student, User
from utils.auth import create_access_token
from utils.taipei_time import now_taipei_naive


@pytest.fixture
def activity_client(tmp_path):
    db_path = tmp_path / "refunded.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=db_engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)
    app = FastAPI()
    from utils.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(parent_portal_router)
    from api.parent_portal._dependencies import get_parent_db
    from tests._parent_rls_test_utils import (
        make_sqlite_parent_db_override,
        register_sqlite_parent_rls_udfs,
    )

    register_sqlite_parent_rls_udfs(db_engine)
    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(
        session_factory
    )
    with TestClient(app) as client:
        yield client, session_factory
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    db_engine.dispose()


def _setup(session, line_user_id="UA"):
    user = User(
        username=f"p_{line_user_id}",
        password_hash="!LINE_ONLY",
        role="parent",
        permission_names=[],
        is_active=True,
        line_user_id=line_user_id,
        token_version=0,
    )
    session.add(user)
    session.flush()
    classroom = Classroom(name="班", is_active=True)
    session.add(classroom)
    session.flush()
    student = Student(
        student_id="S1", name="童", classroom_id=classroom.id, is_active=True
    )
    session.add(student)
    session.flush()
    session.add(
        Guardian(
            student_id=student.id,
            user_id=user.id,
            name="父",
            phone="0911",
            relation="父",
            is_primary=True,
        )
    )
    session.flush()
    reg = ActivityRegistration(
        student_name=student.name,
        is_active=True,
        school_year=115,
        semester=1,
        student_id=student.id,
        parent_phone="0911",
        pending_review=False,
        match_status="manual",
        paid_amount=0,
        is_paid=False,
    )
    session.add(reg)
    session.flush()
    return user, reg


def _pay(session, reg, *, type_, amount, voided=False):
    rec = ActivityPaymentRecord(
        registration_id=reg.id,
        type=type_,
        amount=amount,
        payment_date=date(2026, 4, 1),
        payment_method="現金",
        voided_at=now_taipei_naive() if voided else None,
    )
    session.add(rec)
    session.flush()


def _token(user):
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permission_names": [],
            "token_version": 0,
        }
    )


def _get_reg(client, token):
    resp = client.get(
        "/api/parent/activity/my-registrations", cookies={"access_token": token}
    )
    assert resp.status_code == 200
    return resp.json()["items"][0]


class TestRefundedAmount:
    def test_sums_active_refunds(self, activity_client):
        client, sf = activity_client
        with sf() as s:
            user, reg = _setup(s)
            _pay(s, reg, type_="payment", amount=5000)
            _pay(s, reg, type_="refund", amount=3000)
            _pay(s, reg, type_="refund", amount=2000)
            s.commit()
            token = _token(user)
        item = _get_reg(client, token)
        assert item["refunded_amount"] == 5000

    def test_excludes_voided_refunds(self, activity_client):
        client, sf = activity_client
        with sf() as s:
            user, reg = _setup(s)
            _pay(s, reg, type_="refund", amount=1000)
            _pay(s, reg, type_="refund", amount=2000, voided=True)  # 作廢不計
            s.commit()
            token = _token(user)
        item = _get_reg(client, token)
        assert item["refunded_amount"] == 1000

    def test_no_refund_is_zero(self, activity_client):
        # 「從未繳」：無任何退費 → refunded_amount=0（與「退過費歸零」可分辨）
        client, sf = activity_client
        with sf() as s:
            user, reg = _setup(s)
            _pay(s, reg, type_="payment", amount=1000)
            s.commit()
            token = _token(user)
        item = _get_reg(client, token)
        assert item["refunded_amount"] == 0
