"""refund-suggest endpoint 測試"""

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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.fees import router as fees_router
from models.base import Base
from models.classroom import ClassGrade, Classroom, Student, LIFECYCLE_ACTIVE
from models.database import User
from models.fees import FeeItem, FeeTemplate, StudentFeeRecord
from models.student_leave import StudentLeaveRequest
from utils.auth import hash_password

# ---------------------------------------------------------------------------
# Fixtures: app + DB + admin client（沿用 test_fee_templates.py 模式）
# ---------------------------------------------------------------------------


@pytest.fixture
def _backend(tmp_path):
    """檔案型 SQLite engine，swap global _engine/_SessionFactory。"""
    db_path = tmp_path / "refund_suggest.sqlite"
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

    yield {
        "engine": engine,
        "session_factory": session_factory,
        "app": app,
    }

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def session(_backend):
    """測試用 ORM session（與 API 共用同一 engine）。"""
    s = _backend["session_factory"]()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client_admin(_backend):
    """已登入的 admin 帳號 client（permissions=-1 表全開）。"""
    with _backend["session_factory"]() as s:
        u = User(
            username="refund_admin",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permissions=-1,
            is_active=True,
        )
        s.add(u)
        s.commit()

    client = TestClient(_backend["app"])
    r = client.post(
        "/api/auth/login",
        json={"username": "refund_admin", "password": "Temp123456"},
    )
    assert r.status_code == 200, f"admin login failed: {r.text}"
    yield client
    client.close()


@pytest.fixture
def setup_fee_record(session):
    """建一個學生 + 一筆已繳註冊費記錄（period=114-1）。"""
    g = ClassGrade(name="大班", is_active=True, sort_order=3)
    session.add(g)
    session.flush()
    c = Classroom(
        name="大A", school_year=114, semester=1, grade_id=g.id, is_active=True
    )
    session.add(c)
    session.flush()
    s = Student(
        student_id="S001",
        name="小明",
        classroom_id=c.id,
        lifecycle_status=LIFECYCLE_ACTIVE,
        is_active=True,
        enrollment_date=date(2025, 8, 1),
    )
    session.add(s)
    fi = FeeItem(name="114-1 註冊費", amount=19000, period="114-1", is_active=True)
    session.add(fi)
    session.flush()
    rec = StudentFeeRecord(
        student_id=s.id,
        student_name="小明",
        classroom_name="大A",
        fee_item_id=fi.id,
        fee_item_name="114-1 註冊費",
        amount_due=19000,
        amount_paid=19000,
        status="paid",
        payment_date=date(2025, 8, 5),
        period="114-1",
        fee_type="registration",
    )
    session.add(rec)
    session.commit()
    return {"student": s, "record": rec, "classroom": c}


def test_suggest_enrollment_under_one_third(client_admin, setup_fee_record):
    """8/1 入學, 9/15 離園, 學期 8/1-1/31 ≈ 教保日 ~100 天, 已服務 ~30 天 < 1/3 → 退 2/3"""
    rec = setup_fee_record["record"]
    r = client_admin.post(
        f"/api/fees/records/{rec.id}/refund-suggest",
        json={"withdrawal_date": "2025-09-15"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["calc_method"] == "enrollment_ratio"
    assert body["calc_payload"]["refund_ratio"] == "2/3"
    assert body["suggested_amount"] == round(19000 * 2 / 3)


def test_suggest_with_overrides(client_admin, setup_fee_record):
    rec = setup_fee_record["record"]
    r = client_admin.post(
        f"/api/fees/records/{rec.id}/refund-suggest",
        json={
            "withdrawal_date": "2025-09-15",
            "T_total_override": 200,
            "T_served_override": 50,
        },
    )
    body = r.json()
    # 50/200 = 0.25 < 1/3 → 退 2/3
    assert body["calc_payload"]["T_total"] == 200
    assert body["calc_payload"]["T_served"] == 50


def test_suggest_monthly_with_consecutive_leave(
    client_admin, session, setup_fee_record
):
    """月費 + 該月連續請假 ≥5 上課日 → 算建議退費。"""
    s = setup_fee_record["student"]
    # 建月費 record
    fi = FeeItem(name="114-1 9月 月費", amount=13000, period="114-1")
    session.add(fi)
    session.flush()
    rec = StudentFeeRecord(
        student_id=s.id,
        student_name=s.name,
        classroom_name="大A",
        fee_item_id=fi.id,
        fee_item_name="114-1 9月 月費",
        amount_due=13000,
        amount_paid=13000,
        status="paid",
        payment_date=date(2025, 9, 5),
        period="114-1",
        fee_type="monthly",
        target_month="2025-09",
    )
    session.add(rec)
    # 加事先請假:9/1-9/12 連續工作日(週一~週五 + 週一~週五 = 約 10 天)
    lr = StudentLeaveRequest(
        student_id=s.id,
        applicant_user_id=1,
        leave_type="病假",
        start_date=date(2025, 9, 1),
        end_date=date(2025, 9, 12),
        status="approved",
        created_at=datetime(2025, 8, 25),  # 事先 (created < start)
        reviewed_at=datetime(2025, 8, 26),
    )
    session.add(lr)
    session.commit()

    r = client_admin.post(
        f"/api/fees/records/{rec.id}/refund-suggest",
        json={"withdrawal_date": "2025-09-30"},
    )
    body = r.json()
    assert body["calc_method"] == "monthly_partial"
    assert body["suggested_amount"] > 0
    assert body["calc_payload"]["L_consecutive"] >= 5


def test_suggest_monthly_not_advance_filed(client_admin, session, setup_fee_record):
    """當天才補請假 → advance_filed=False → 退 0。"""
    s = setup_fee_record["student"]
    fi = FeeItem(name="114-1 9月 月費", amount=13000, period="114-1")
    session.add(fi)
    session.flush()
    rec = StudentFeeRecord(
        student_id=s.id,
        student_name=s.name,
        classroom_name="大A",
        fee_item_id=fi.id,
        fee_item_name="114-1 9月 月費",
        amount_due=13000,
        amount_paid=13000,
        status="paid",
        payment_date=date(2025, 9, 5),
        period="114-1",
        fee_type="monthly",
        target_month="2025-09",
    )
    session.add(rec)
    lr = StudentLeaveRequest(
        student_id=s.id,
        applicant_user_id=1,
        leave_type="事假",
        start_date=date(2025, 9, 1),
        end_date=date(2025, 9, 12),
        status="approved",
        created_at=datetime(2025, 9, 1, 8, 0),  # 同日才申請
        reviewed_at=datetime(2025, 9, 1, 9, 0),
    )
    session.add(lr)
    session.commit()

    r = client_admin.post(
        f"/api/fees/records/{rec.id}/refund-suggest",
        json={"withdrawal_date": "2025-09-30"},
    )
    body = r.json()
    assert body["suggested_amount"] == 0
    assert any("事先" in w for w in body["warnings"])


def test_suggest_material_fee_blocked(client_admin, session, setup_fee_record):
    """代購品 → 不退,suggested_amount=0 + warning。"""
    s = setup_fee_record["student"]
    fi = FeeItem(name="教材費", amount=2000, period="114-1")
    session.add(fi)
    session.flush()
    rec = StudentFeeRecord(
        student_id=s.id,
        student_name=s.name,
        classroom_name="大A",
        fee_item_id=fi.id,
        fee_item_name="教材費",
        amount_due=2000,
        amount_paid=2000,
        status="paid",
        period="114-1",
        fee_type="material",
    )
    session.add(rec)
    session.commit()

    r = client_admin.post(
        f"/api/fees/records/{rec.id}/refund-suggest",
        json={"withdrawal_date": "2025-09-15"},
    )
    body = r.json()
    assert body["suggested_amount"] == 0
    assert body["calc_method"] == "no_refund"
    assert any("代購品" in w or "不退" in w for w in body["warnings"])


def test_suggest_record_not_found(client_admin):
    r = client_admin.post(
        "/api/fees/records/999999/refund-suggest",
        json={"withdrawal_date": "2025-09-15"},
    )
    assert r.status_code == 404
