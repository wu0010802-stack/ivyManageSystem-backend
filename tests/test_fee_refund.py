"""tests/test_fee_refund.py — 學費退款流程。

涵蓋：
- POST /records/{id}/refund 成功扣減 amount_paid，寫入 StudentFeeRefund
- 退款超過已繳 → 400
- 多次退款累積至 amount_paid=0 後 status='unpaid'
- GET /records/{id}/refunds 列歷史
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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.fees import router as fees_router
from models.base import Base
from models.classroom import Classroom, Student
from models.database import User
from models.fees import FeeItem, StudentFeeRecord, StudentFeeRefund
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "refund.sqlite"
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
    app.include_router(fees_router)

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _admin(session):
    user = User(
        username="refund_admin",
        password_hash=hash_password("Temp123456"),
        role="admin",
        permissions=Permission.FEES_READ | Permission.FEES_WRITE,
        is_active=True,
    )
    session.add(user)
    session.flush()


def _login(c: TestClient):
    return c.post(
        "/api/auth/login",
        json={"username": "refund_admin", "password": "Temp123456"},
    )


def _seed(session, *, amount_due=1000, amount_paid=800, status="partial"):
    cls = Classroom(name="大班", school_year=2025, semester=1)
    session.add(cls)
    session.flush()
    st = Student(student_id="R0001", name="李小華", is_active=True, classroom_id=cls.id)
    session.add(st)
    session.flush()
    item = FeeItem(name="學費", amount=amount_due, period="2025-1", is_active=True)
    session.add(item)
    session.flush()
    rec = StudentFeeRecord(
        student_id=st.id,
        student_name=st.name,
        classroom_name=cls.name,
        fee_item_id=item.id,
        fee_item_name=item.name,
        amount_due=amount_due,
        amount_paid=amount_paid,
        status=status,
        period="2025-1",
    )
    session.add(rec)
    session.flush()
    return rec


class TestRefundEndpoint:
    def test_refund_creates_history_and_reduces_paid(self, client):
        c, sf = client
        with sf() as s:
            _admin(s)
            rec = _seed(s, amount_due=1000, amount_paid=800, status="partial")
            s.commit()
            rec_id = rec.id

        assert _login(c).status_code == 200
        res = c.post(
            f"/api/fees/records/{rec_id}/refund",
            json={"amount": 300, "reason": "家長申請退學", "notes": "七月前退"},
        )
        assert res.status_code == 201, res.text
        data = res.json()
        assert data["refund_amount"] == 300
        assert data["new_amount_paid"] == 500
        assert data["status"] == "partial"

        with sf() as s:
            rec = s.query(StudentFeeRecord).filter_by(id=rec_id).one()
            assert rec.amount_paid == 500
            refunds = s.query(StudentFeeRefund).filter_by(record_id=rec_id).all()
            assert len(refunds) == 1
            assert refunds[0].amount == 300
            assert refunds[0].reason == "家長申請退學"
            # operator 來自 JWT payload，fixture 沒建立 Employee 所以 name 為空 → 走 "unknown" fallback
            assert refunds[0].refunded_by in ("refund_admin", "unknown")

    def test_refund_exceeds_paid_returns_400(self, client):
        c, sf = client
        with sf() as s:
            _admin(s)
            rec = _seed(s, amount_paid=500)
            s.commit()
            rec_id = rec.id

        assert _login(c).status_code == 200
        res = c.post(
            f"/api/fees/records/{rec_id}/refund",
            json={"amount": 600, "reason": "誤收"},
        )
        assert res.status_code == 400
        assert "超過已繳金額" in res.json()["detail"]

        with sf() as s:
            rec = s.query(StudentFeeRecord).filter_by(id=rec_id).one()
            assert rec.amount_paid == 500
            assert s.query(StudentFeeRefund).count() == 0

    def test_refund_to_zero_sets_status_unpaid(self, client):
        c, sf = client
        with sf() as s:
            _admin(s)
            rec = _seed(s, amount_due=1000, amount_paid=1000, status="paid")
            s.commit()
            rec_id = rec.id

        assert _login(c).status_code == 200
        res = c.post(
            f"/api/fees/records/{rec_id}/refund",
            json={"amount": 1000, "reason": "全額退"},
        )
        assert res.status_code == 201, res.text
        assert res.json()["status"] == "unpaid"
        assert res.json()["new_amount_paid"] == 0

    def test_refund_multiple_times_accumulates_history(self, client):
        c, sf = client
        with sf() as s:
            _admin(s)
            rec = _seed(s, amount_due=1000, amount_paid=900, status="partial")
            s.commit()
            rec_id = rec.id

        assert _login(c).status_code == 200
        for amt, reason in [(200, "第一次退"), (300, "第二次退"), (400, "第三次退")]:
            res = c.post(
                f"/api/fees/records/{rec_id}/refund",
                json={"amount": amt, "reason": reason},
            )
            assert res.status_code == 201, res.text

        res = c.get(f"/api/fees/records/{rec_id}/refunds")
        assert res.status_code == 200
        data = res.json()
        assert data["total_refunded"] == 900
        assert len(data["refunds"]) == 3
        # 依 refunded_at desc，最新在前
        assert data["refunds"][0]["reason"] == "第三次退"

    def test_refund_without_paid_amount_returns_400(self, client):
        c, sf = client
        with sf() as s:
            _admin(s)
            rec = _seed(s, amount_due=1000, amount_paid=0, status="unpaid")
            s.commit()
            rec_id = rec.id

        assert _login(c).status_code == 200
        res = c.post(
            f"/api/fees/records/{rec_id}/refund",
            json={"amount": 100, "reason": "誤退"},
        )
        assert res.status_code == 400
        assert "尚未有任何繳費" in res.json()["detail"]


class TestRefundInputValidation:
    def test_amount_must_be_positive(self, client):
        c, sf = client
        with sf() as s:
            _admin(s)
            rec = _seed(s, amount_paid=500)
            s.commit()
            rec_id = rec.id

        assert _login(c).status_code == 200
        res = c.post(
            f"/api/fees/records/{rec_id}/refund",
            json={"amount": 0, "reason": "test"},
        )
        assert res.status_code == 422

    def test_reason_required(self, client):
        c, sf = client
        with sf() as s:
            _admin(s)
            rec = _seed(s, amount_paid=500)
            s.commit()
            rec_id = rec.id

        assert _login(c).status_code == 200
        res = c.post(
            f"/api/fees/records/{rec_id}/refund",
            json={"amount": 100, "reason": ""},
        )
        assert res.status_code == 422
