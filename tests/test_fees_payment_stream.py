"""P1-B 回歸測試：學費 append-only 繳費流水（StudentFeePayment）

證明舊設計的三個錯帳情境都被根治：
1. 分期繳費：每期金額各歸各月，不會被覆寫到最後一次付款月份
2. 退款後：原月份收入不會消失（StudentFeePayment 不動，退款另走 StudentFeeRefund）
3. partial 狀態：現金確實入帳（不再以 status='paid' 過濾）
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
from models.fees import (
    FeeItem,
    StudentFeePayment,
    StudentFeeRecord,
    StudentFeeRefund,
)
from services import finance_report_service as svc
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def fee_stream_client(tmp_path):
    db_path = tmp_path / "stream.sqlite"
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
    app.include_router(fees_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _admin(session):
    u = User(
        username="stream_admin",
        password_hash=hash_password("Temp123456"),
        role="admin",
        permissions=-1,
        is_active=True,
    )
    session.add(u)
    session.flush()
    return u


def _login(client):
    return client.post(
        "/api/auth/login",
        json={"username": "stream_admin", "password": "Temp123456"},
    )


def _seed_record(session, *, amount_due=1000):
    cls = Classroom(name="大班", school_year=2025, semester=1)
    session.add(cls)
    session.flush()
    st = Student(
        student_id="S00001", name="王小明", is_active=True, classroom_id=cls.id
    )
    session.add(st)
    session.flush()
    item = FeeItem(name="學費", amount=amount_due, period="2026-1", is_active=True)
    session.add(item)
    session.flush()
    rec = StudentFeeRecord(
        student_id=st.id,
        student_name=st.name,
        classroom_name=cls.name,
        fee_item_id=item.id,
        fee_item_name=item.name,
        amount_due=amount_due,
        amount_paid=0,
        status="unpaid",
        period=item.period,
    )
    session.add(rec)
    session.flush()
    return rec


# ══════════════════════════════════════════════════════════════════════
# 情境 1: 分期繳費不會錯月份
# ══════════════════════════════════════════════════════════════════════


class TestInstalmentStaysInOriginalMonth:
    def test_two_instalments_across_months_aggregates_per_month(
        self, fee_stream_client
    ):
        """3/10 繳 500、4/5 補到 1000；月報應 3 月 500、4 月 500，而非 4 月 1000。"""
        client, sf = fee_stream_client
        with sf() as s:
            _admin(s)
            rec = _seed_record(s, amount_due=1000)
            s.commit()
            rec_id = rec.id

        assert _login(client).status_code == 200

        # 第一次繳 500（3/10）
        r1 = client.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": "2026-03-10",
                "amount_paid": 500,
                "payment_method": "現金",
            },
        )
        assert r1.status_code == 200, r1.text

        # 第二次補到 1000（4/5）
        r2 = client.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": "2026-04-05",
                "amount_paid": 1000,
                "payment_method": "現金",
            },
        )
        assert r2.status_code == 200, r2.text

        # 財報：3 月 500、4 月 500；不是 4 月 1000
        with sf() as s:
            monthly = svc.get_tuition_revenue_by_month(s, 2026)
        assert monthly.get(3) == 500, f"3 月應 500，實得 {monthly}"
        assert monthly.get(4) == 500, f"4 月應 500，實得 {monthly}"

        # StudentFeePayment 應有兩筆
        with sf() as s:
            payments = s.query(StudentFeePayment).filter_by(record_id=rec_id).all()
            assert len(payments) == 2
            assert sorted(p.amount for p in payments) == [500, 500]
            assert sorted(p.payment_date for p in payments) == [
                date(2026, 3, 10),
                date(2026, 4, 5),
            ]


# ══════════════════════════════════════════════════════════════════════
# 情境 2: 退款後原月份收入不會消失
# ══════════════════════════════════════════════════════════════════════


class TestRefundDoesNotEraseRevenue:
    def test_refund_after_paid_keeps_original_month_revenue(self, fee_stream_client):
        """3/10 繳清 1000；4/2 退 300。3 月收入應仍為 1000、4 月退款 300。"""
        client, sf = fee_stream_client
        with sf() as s:
            _admin(s)
            rec = _seed_record(s, amount_due=1000)
            s.commit()
            rec_id = rec.id

        assert _login(client).status_code == 200

        # 3/10 繳清
        r_pay = client.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": "2026-03-10",
                "amount_paid": 1000,
                "payment_method": "現金",
            },
        )
        assert r_pay.status_code == 200

        # 4/2 退 300（refund 需 reason ≥5 字 + 小額 ≤1000 不需簽核）
        r_refund = client.post(
            f"/api/fees/records/{rec_id}/refund",
            json={"amount": 300, "reason": "家長請假退部分費用"},
        )
        assert r_refund.status_code == 201, r_refund.text

        with sf() as s:
            revenue = svc.get_tuition_revenue_by_month(s, 2026)
            refund = svc.get_tuition_refund_by_month(s, 2026)
            rec = s.query(StudentFeeRecord).get(rec_id)
        # 關鍵：3 月收入 1000 未消失（舊設計退款會讓 status 變 partial → 月報 0）
        assert revenue.get(3) == 1000, f"3 月收入應保留 1000，實得 {revenue}"
        # StudentFeePayment 仍在、未受退款影響
        with sf() as s:
            payments = s.query(StudentFeePayment).filter_by(record_id=rec_id).all()
            assert len(payments) == 1
            assert payments[0].amount == 1000
        # amount_paid 快照反映淨額（1000 - 300 = 700）
        assert rec.amount_paid == 700
        # 退款該月（本測試執行當月）在 refund 總表有紀錄 ≥ 300
        assert sum(refund.values()) >= 300


# ══════════════════════════════════════════════════════════════════════
# 情境 3: partial 狀態的現金仍入帳
# ══════════════════════════════════════════════════════════════════════


class TestPartialStatusStillCountsAsRevenue:
    def test_partial_payment_appears_in_monthly_revenue(self, fee_stream_client):
        """繳 500（status=partial），月報仍應入帳 500（不再以 status='paid' 過濾）。"""
        client, sf = fee_stream_client
        with sf() as s:
            _admin(s)
            rec = _seed_record(s, amount_due=1000)
            s.commit()
            rec_id = rec.id

        assert _login(client).status_code == 200
        res = client.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": "2026-03-15",
                "amount_paid": 500,
                "payment_method": "現金",
            },
        )
        assert res.status_code == 200

        with sf() as s:
            rec = s.query(StudentFeeRecord).get(rec_id)
            assert rec.status == "partial"
            revenue = svc.get_tuition_revenue_by_month(s, 2026)
        assert revenue.get(3) == 500, f"partial 現金應入帳 500，實得 {revenue}"


# ══════════════════════════════════════════════════════════════════════
# 情境 4: pay_fee_record 冪等（同 key 重送不雙扣）
# ══════════════════════════════════════════════════════════════════════


class TestPayFeeRecordIdempotency:
    def test_same_key_replays_without_double_insert(self, fee_stream_client):
        """同 idempotency_key 重送回放，不會建第二筆 StudentFeePayment。"""
        client, sf = fee_stream_client
        with sf() as s:
            _admin(s)
            rec = _seed_record(s, amount_due=1000)
            s.commit()
            rec_id = rec.id

        assert _login(client).status_code == 200

        body = {
            "payment_date": "2026-03-15",
            "amount_paid": 400,
            "payment_method": "現金",
            "idempotency_key": "fee-pay-k-001",
        }
        r1 = client.put(f"/api/fees/records/{rec_id}/pay", json=body)
        assert r1.status_code == 200, r1.text
        r2 = client.put(f"/api/fees/records/{rec_id}/pay", json=body)
        assert r2.status_code == 200, r2.text

        with sf() as s:
            payments = s.query(StudentFeePayment).filter_by(record_id=rec_id).all()
            assert len(payments) == 1
            rec = s.query(StudentFeeRecord).get(rec_id)
            assert rec.amount_paid == 400

    def test_same_key_different_record_returns_409(self, fee_stream_client):
        """同 key 用於不同 record（上下文不同）→ 409，避免錯帳到其他 record。"""
        client, sf = fee_stream_client
        with sf() as s:
            _admin(s)
            rec1 = _seed_record(s, amount_due=1000)
            # 第二個 record：不同 fee_item 避免 uniqueness
            cls = s.query(Classroom).first()
            st = s.query(Student).first()
            item2 = FeeItem(name="雜費", amount=500, period="2026-1", is_active=True)
            s.add(item2)
            s.flush()
            rec2 = StudentFeeRecord(
                student_id=st.id,
                student_name=st.name,
                classroom_name=cls.name,
                fee_item_id=item2.id,
                fee_item_name=item2.name,
                amount_due=500,
                amount_paid=0,
                status="unpaid",
                period=item2.period,
            )
            s.add(rec2)
            s.flush()
            s.commit()
            rec1_id, rec2_id = rec1.id, rec2.id

        assert _login(client).status_code == 200

        r1 = client.put(
            f"/api/fees/records/{rec1_id}/pay",
            json={
                "payment_date": "2026-03-15",
                "amount_paid": 300,
                "payment_method": "現金",
                "idempotency_key": "cross-rec-key",
            },
        )
        assert r1.status_code == 200

        # 同 key 用於 rec2 → 409
        r2 = client.put(
            f"/api/fees/records/{rec2_id}/pay",
            json={
                "payment_date": "2026-03-16",
                "amount_paid": 300,
                "payment_method": "現金",
                "idempotency_key": "cross-rec-key",
            },
        )
        assert r2.status_code == 409, r2.text
