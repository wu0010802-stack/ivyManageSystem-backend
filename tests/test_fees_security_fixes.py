"""tests/test_fees_security_fixes.py — 學費端點安全修復回歸測試。

涵蓋：
- PayRequest.amount_paid 上限 MAX_FEE_AMOUNT
- 拒絕調降 amount_paid（除非 allow_decrease=True）
- 稽核 summary 包含前後值
- InsuranceService 拒絕負薪資
（c2: FeeItem.amount cap 隨 /api/fees/items endpoint 一同退場）
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
from api.fees import MAX_FEE_AMOUNT, router as fees_router
from models.base import Base
from models.classroom import Classroom, Student
from models.fees import StudentFeeRecord
from models.database import User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "fees.sqlite"
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


def _admin(session, username="fees_admin"):
    user = User(
        username=username,
        password_hash=hash_password("Temp123456"),
        role="admin",
        permission_names=["FEES_READ", "FEES_WRITE"],
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(c: TestClient, username="fees_admin"):
    return c.post(
        "/api/auth/login", json={"username": username, "password": "Temp123456"}
    )


def _seed_record(session, *, amount_due=1000, amount_paid=0, status="unpaid"):
    cls = Classroom(name="大班", school_year=2025, semester=1)
    session.add(cls)
    session.flush()
    st = Student(
        student_id="S00001", name="王小明", is_active=True, classroom_id=cls.id
    )
    session.add(st)
    session.flush()
    rec = StudentFeeRecord(
        student_id=st.id,
        student_name=st.name,
        classroom_name=cls.name,
        fee_item_name="學費",
        amount_due=amount_due,
        amount_paid=amount_paid,
        status=status,
        period="2025-1",
    )
    session.add(rec)
    session.flush()
    return rec


# c2: TestFeeItemAmountCap 已隨 /api/fees/items endpoint 一同退場
# MAX_FEE_AMOUNT 的 schema-level 守衛改由 PayRequest.amount_paid 的 422 cap 驗證
# （見 TestPayRecordAmountCap.test_amount_above_cap_rejected）。


class TestPayRecordDecreaseBlocked:
    def test_reject_decrease_without_flag(self, client):
        c, sf = client
        with sf() as s:
            _admin(s)
            rec = _seed_record(s, amount_due=1000, amount_paid=800, status="partial")
            s.commit()
            rec_id = rec.id

        assert _login(c).status_code == 200
        res = c.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": date.today().isoformat(),
                "amount_paid": 300,  # 低於已登記 800
                "payment_method": "現金",
            },
        )
        assert res.status_code == 400
        assert "低於已登記金額" in res.json()["detail"]

        # 確保 DB 未被改動
        with sf() as s:
            r = s.query(StudentFeeRecord).filter_by(id=rec_id).one()
            assert r.amount_paid == 800
            assert r.status == "partial"

    def test_decrease_must_go_through_refund_flow(self, client):
        """調降金額必須走 POST /refund，pay 端點不再接受任何方式的調降。"""
        c, sf = client
        with sf() as s:
            _admin(s)
            rec = _seed_record(s, amount_due=1000, amount_paid=800, status="partial")
            s.commit()
            rec_id = rec.id

        assert _login(c).status_code == 200
        # 即使帶任何舊的 allow_decrease payload，仍應被拒絕（欄位已不存在）
        res = c.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": date.today().isoformat(),
                "amount_paid": 300,
                "payment_method": "現金",
                "allow_decrease": True,
            },
        )
        assert res.status_code == 400
        assert "請改用退款流程" in res.json()["detail"]

    def test_increase_allowed_as_normal_flow(self, client):
        c, sf = client
        with sf() as s:
            _admin(s)
            rec = _seed_record(s, amount_due=1000, amount_paid=500, status="partial")
            s.commit()
            rec_id = rec.id

        assert _login(c).status_code == 200
        res = c.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": date.today().isoformat(),
                "amount_paid": 1000,
                "payment_method": "現金",
            },
        )
        assert res.status_code == 200, res.text
        with sf() as s:
            r = s.query(StudentFeeRecord).filter_by(id=rec_id).one()
            assert r.amount_paid == 1000
            assert r.status == "paid"


class TestPayRecordCumulativeApprovalThreshold:
    """C5：登記繳費的金流簽核應以「該 record 既有累計已繳 + 本次 delta」
    之新累計判定，防止把大筆收款拆成多次 < 50,000 的 delta 繞過簽核。"""

    def test_split_payments_cross_threshold_requires_approve(self, client):
        c, sf = client
        with sf() as s:
            _admin(s)  # 僅 FEES_READ/WRITE，無 ACTIVITY_PAYMENT_APPROVE
            rec = _seed_record(s, amount_due=100_000, amount_paid=0, status="unpaid")
            s.commit()
            rec_id = rec.id

        assert _login(c).status_code == 200
        # 第一筆 delta=40,000（累計 40,000 < 50,000）→ 放行
        res1 = c.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": date.today().isoformat(),
                "amount_paid": 40_000,
                "payment_method": "現金",
            },
        )
        assert res1.status_code == 200, res1.text

        # 第二筆 delta=40,000，但「累計已繳」推進到 80,000 ≥ 50,000 → 需簽核 → 403
        res2 = c.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": date.today().isoformat(),
                "amount_paid": 80_000,
                "payment_method": "現金",
            },
        )
        assert res2.status_code == 403, res2.text

        # DB 不應落到 80,000（第二筆被擋）
        with sf() as s:
            r = s.query(StudentFeeRecord).filter_by(id=rec_id).one()
            assert r.amount_paid == 40_000

    def test_normal_monthly_fee_below_threshold_unaffected(self, client):
        """常規月費 amount_due < 門檻：一次繳清不需簽核，不受累計判定影響。"""
        c, sf = client
        with sf() as s:
            _admin(s)
            rec = _seed_record(s, amount_due=12_000, amount_paid=0, status="unpaid")
            s.commit()
            rec_id = rec.id

        assert _login(c).status_code == 200
        res = c.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": date.today().isoformat(),
                "amount_paid": 12_000,
                "payment_method": "現金",
            },
        )
        assert res.status_code == 200, res.text


class TestPayRecordAmountCap:
    def test_amount_above_cap_rejected(self, client):
        c, sf = client
        with sf() as s:
            _admin(s)
            rec = _seed_record(s, amount_due=MAX_FEE_AMOUNT)
            s.commit()
            rec_id = rec.id

        assert _login(c).status_code == 200
        res = c.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": date.today().isoformat(),
                "amount_paid": MAX_FEE_AMOUNT + 1,
                "payment_method": "現金",
            },
        )
        assert res.status_code == 422


class TestInsuranceNegativeSalary:
    def test_negative_salary_raises(self):
        from services.insurance_service import InsuranceService

        svc = InsuranceService()
        with pytest.raises(ValueError, match="投保薪資不可為負數"):
            svc.calculate(salary=-100)

    def test_invalid_pension_self_rate_raises(self):
        from services.insurance_service import InsuranceService

        svc = InsuranceService()
        with pytest.raises(ValueError, match="勞退自提比例"):
            svc.calculate(salary=30000, pension_self_rate=0.1)
