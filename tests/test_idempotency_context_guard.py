"""P2 回歸：idempotency_key 跨模組上下文守衛。

以前的語意：UNIQUE 永久唯一 + handler 查詢限 10 分鐘 window，衝突導致：
- window 外同 key 重送 → helper 找不到 → INSERT → UNIQUE 拒絕 → 500
- registrations.py fallback 沒驗證 registration_id，回錯 record 的資料

修正後：
- 查詢移除時間 window（改為全域）
- replay 前強制驗證 (registration_id / record_id, amount, type) 相符
- 不符回 409（key 誤用），非 500 或錯帳 replay
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
from api.fees import router as fees_router
from models.base import Base
from models.classroom import Classroom, Student
from models.database import (
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    RegistrationCourse,
    User,
)
from models.fees import FeeItem, StudentFeePayment, StudentFeeRecord, StudentFeeRefund
from utils.auth import hash_password
from utils.permissions import Permission

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def idem_client(tmp_path):
    db_path = tmp_path / "idem.sqlite"
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
    app.include_router(fees_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _admin(session, username="idem_admin"):
    user = User(
        username=username,
        password_hash=hash_password("Temp123456"),
        role="admin",
        permissions=-1,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username="idem_admin"):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": "Temp123456"},
    )


def _setup_activity_reg(session, *, student_name="王小明", paid_amount=500):
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    course = ActivityCourse(
        name=f"課程_{student_name}",
        price=1000,
        capacity=30,
        school_year=sy,
        semester=sem,
    )
    session.add(course)
    session.flush()
    reg = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name="大班",
        paid_amount=paid_amount,
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
    return reg


def _setup_fee_record(session, *, amount_due=1000, amount_paid=1000):
    cls = Classroom(name="大班", school_year=2025, semester=1)
    session.add(cls)
    session.flush()
    st = Student(
        student_id=f"S{session.query(Student).count():05d}",
        name=f"學生{session.query(Student).count()}",
        is_active=True,
        classroom_id=cls.id,
    )
    session.add(st)
    session.flush()
    item = FeeItem(
        name="學費",
        amount=amount_due,
        period="2026-1",
        is_active=True,
    )
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
        status="paid" if amount_paid >= amount_due else "partial",
        period=item.period,
        payment_date=date(2026, 3, 1),
        payment_method="現金",
    )
    session.add(rec)
    session.flush()
    return rec


# ══════════════════════════════════════════════════════════════════════
# activity add_registration_payment
# ══════════════════════════════════════════════════════════════════════


class TestActivityRegistrationPaymentIdempotency:
    def test_same_key_different_registration_returns_409(self, idem_client):
        """同 key 跨不同 registration → 409，不會誤 replay 回錯 registration 的資料。"""
        client, sf = idem_client
        with sf() as s:
            _admin(s)
            r1 = _setup_activity_reg(s, student_name="甲", paid_amount=0)
            r2 = _setup_activity_reg(s, student_name="乙", paid_amount=0)
            s.commit()
            r1_id, r2_id = r1.id, r2.id

        assert _login(client).status_code == 200

        # 先對 r1 成功建 payment
        r1_pay = client.post(
            f"/api/activity/registrations/{r1_id}/payments",
            json={
                "type": "payment",
                "amount": 500,
                "payment_date": date.today().isoformat(),
                "payment_method": "現金",
                "idempotency_key": "cross-reg-key-001",
            },
        )
        assert r1_pay.status_code == 201

        # 同 key 用到 r2（amount 也不同）→ 409
        r2_pay = client.post(
            f"/api/activity/registrations/{r2_id}/payments",
            json={
                "type": "payment",
                "amount": 700,
                "payment_date": date.today().isoformat(),
                "payment_method": "現金",
                "idempotency_key": "cross-reg-key-001",
            },
        )
        assert r2_pay.status_code == 409, r2_pay.text
        # r2 的 paid_amount 沒被污染
        with sf() as s:
            r2 = s.query(ActivityRegistration).get(r2_id)
            assert (r2.paid_amount or 0) == 0

    def test_same_key_different_amount_returns_409(self, idem_client):
        """同 key 但 amount 不符 → 409（避免 replay 金額錯誤）。"""
        client, sf = idem_client
        with sf() as s:
            _admin(s)
            reg = _setup_activity_reg(s, paid_amount=0)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        first = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={
                "type": "payment",
                "amount": 300,
                "payment_date": date.today().isoformat(),
                "payment_method": "現金",
                "idempotency_key": "same-amt-key",
            },
        )
        assert first.status_code == 201

        second = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={
                "type": "payment",
                "amount": 500,  # 不同金額
                "payment_date": date.today().isoformat(),
                "payment_method": "現金",
                "idempotency_key": "same-amt-key",
            },
        )
        assert second.status_code == 409

    def test_same_key_same_context_replays(self, idem_client):
        """同 key 同 registration 同 amount → replay，不雙扣。"""
        client, sf = idem_client
        with sf() as s:
            _admin(s)
            reg = _setup_activity_reg(s, paid_amount=0)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200
        body = {
            "type": "payment",
            "amount": 500,
            "payment_date": date.today().isoformat(),
            "payment_method": "現金",
            "idempotency_key": "replay-same-key",
        }
        r1 = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        r2 = client.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
        assert r1.status_code == 201
        assert r2.status_code == 201
        with sf() as s:
            records = (
                s.query(ActivityPaymentRecord).filter_by(registration_id=reg_id).all()
            )
            assert len(records) == 1, "不應雙扣"
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 500


# ══════════════════════════════════════════════════════════════════════
# fees refund_fee_record
# ══════════════════════════════════════════════════════════════════════


class TestFeeRefundIdempotency:
    def test_refund_same_key_different_record_returns_409(self, idem_client):
        """退款同 key 跨不同 record → 409。"""
        client, sf = idem_client
        with sf() as s:
            _admin(s)
            r1 = _setup_fee_record(s, amount_due=1000, amount_paid=1000)
            s.flush()
            # 第二個 record 需換另一位 student 避免 uq_student_fee_item
            cls = s.query(Classroom).first()
            st2 = Student(
                student_id="S99999",
                name="另一位學生",
                is_active=True,
                classroom_id=cls.id,
            )
            s.add(st2)
            s.flush()
            item2 = FeeItem(name="雜費", amount=800, period="2026-1", is_active=True)
            s.add(item2)
            s.flush()
            r2 = StudentFeeRecord(
                student_id=st2.id,
                student_name=st2.name,
                classroom_name=cls.name,
                fee_item_id=item2.id,
                fee_item_name=item2.name,
                amount_due=800,
                amount_paid=800,
                status="paid",
                period=item2.period,
                payment_date=date(2026, 3, 1),
                payment_method="現金",
            )
            s.add(r2)
            s.flush()
            s.commit()
            r1_id, r2_id = r1.id, r2.id

        assert _login(client).status_code == 200

        first = client.post(
            f"/api/fees/records/{r1_id}/refund",
            json={
                "amount": 300,
                "reason": "第一次退款測試",
                "idempotency_key": "fee-cross-record-k",
            },
        )
        assert first.status_code == 201

        # 同 key 用到 r2 → 409
        second = client.post(
            f"/api/fees/records/{r2_id}/refund",
            json={
                "amount": 300,
                "reason": "第二次退款測試",
                "idempotency_key": "fee-cross-record-k",
            },
        )
        assert second.status_code == 409

        # r2.amount_paid 未被污染
        with sf() as s:
            r2 = s.query(StudentFeeRecord).get(r2_id)
            assert r2.amount_paid == 800

    def test_refund_same_key_different_amount_returns_409(self, idem_client):
        """退款同 key 同 record 但 amount 不同 → 409。"""
        client, sf = idem_client
        with sf() as s:
            _admin(s)
            rec = _setup_fee_record(s, amount_due=1000, amount_paid=1000)
            s.commit()
            rec_id = rec.id

        assert _login(client).status_code == 200
        first = client.post(
            f"/api/fees/records/{rec_id}/refund",
            json={
                "amount": 300,
                "reason": "第一次退款測試",
                "idempotency_key": "fee-same-key-diff-amt",
            },
        )
        assert first.status_code == 201

        second = client.post(
            f"/api/fees/records/{rec_id}/refund",
            json={
                "amount": 500,
                "reason": "第二次退款測試",
                "idempotency_key": "fee-same-key-diff-amt",
            },
        )
        assert second.status_code == 409

    def test_refund_same_key_out_of_old_window_no_longer_500(self, idem_client):
        """過去 window 外重送 (現在行為：直接 replay)：
        舊版 10 分鐘 window 外會回 500；現在改為全域查詢，
        合法 replay 回 201 + idempotent_replay=True。
        """
        client, sf = idem_client
        with sf() as s:
            _admin(s)
            rec = _setup_fee_record(s, amount_due=1000, amount_paid=1000)
            s.commit()
            rec_id = rec.id

        assert _login(client).status_code == 200
        body = {
            "amount": 300,
            "reason": "window 測試退款",
            "idempotency_key": "fee-out-window",
        }
        first = client.post(f"/api/fees/records/{rec_id}/refund", json=body)
        assert first.status_code == 201

        # 手動把 refunded_at 調到超過舊 window（>10 分鐘）
        from datetime import datetime, timedelta

        with sf() as s:
            r = s.query(StudentFeeRefund).first()
            r.refunded_at = datetime.now() - timedelta(hours=2)
            s.commit()

        # 同 key 同上下文重送 → 以前 window 外會 500，現在應 201 + replay
        second = client.post(f"/api/fees/records/{rec_id}/refund", json=body)
        assert second.status_code == 201, second.text
        assert second.json().get("idempotent_replay") is True
