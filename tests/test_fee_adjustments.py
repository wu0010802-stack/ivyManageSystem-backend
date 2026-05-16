"""學費折抵 (StudentFeeAdjustment) CRUD 測試。

範圍：建立 / 列出（過濾學期、學生）/ 更新 / 刪除 / 權限驗證 / 邊界。
"""

import os
import sys

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
from models.classroom import ClassGrade, Classroom, Student
from models.database import User
from utils.auth import hash_password


@pytest.fixture
def _backend(tmp_path):
    db_path = tmp_path / "adj.sqlite"
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

    yield {"engine": engine, "session_factory": session_factory, "app": app}

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def session(_backend):
    s = _backend["session_factory"]()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client_admin(_backend):
    with _backend["session_factory"]() as s:
        u = User(
            username="adj_admin",
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
        json={"username": "adj_admin", "password": "Temp123456"},
    )
    assert r.status_code == 200, r.text
    yield client
    client.close()


@pytest.fixture
def student_a(session):
    grade = ClassGrade(name="大班", sort_order=3, is_active=True)
    session.add(grade)
    session.flush()
    classroom = Classroom(
        name="大班A",
        class_code="DA-A",
        school_year=114,
        semester=1,
        grade_id=grade.id,
        is_active=True,
    )
    session.add(classroom)
    session.flush()
    student = Student(
        student_id="A001",
        name="王小明",
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.commit()
    return student


def _payload(student_id, **overrides):
    base = {
        "student_id": student_id,
        "period": "114-2",
        "adjustment_type": "sibling_discount",
        "amount": 1300,
        "reason": "兄姊就讀",
    }
    base.update(overrides)
    return base


def test_create_adjustment_success(client_admin, student_a):
    r = client_admin.post("/api/fees/adjustments", json=_payload(student_a.id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["amount"] == 1300
    assert body["adjustment_type"] == "sibling_discount"
    assert body["created_by"] == "adj_admin"
    assert body["id"] > 0


def test_create_invalid_type_rejected(client_admin, student_a):
    r = client_admin.post(
        "/api/fees/adjustments",
        json=_payload(student_a.id, adjustment_type="invalid"),
    )
    assert r.status_code == 422


def test_create_zero_amount_rejected(client_admin, student_a):
    r = client_admin.post(
        "/api/fees/adjustments", json=_payload(student_a.id, amount=0)
    )
    assert r.status_code == 422


def test_create_invalid_period_rejected(client_admin, student_a):
    r = client_admin.post(
        "/api/fees/adjustments", json=_payload(student_a.id, period="114")
    )
    assert r.status_code == 422


def test_list_filter_by_student(client_admin, student_a):
    client_admin.post("/api/fees/adjustments", json=_payload(student_a.id))
    client_admin.post(
        "/api/fees/adjustments",
        json=_payload(student_a.id, adjustment_type="prepayment", amount=5000),
    )
    r = client_admin.get(f"/api/fees/adjustments?student_id={student_a.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    types = {a["adjustment_type"] for a in body["items"]}
    assert types == {"sibling_discount", "prepayment"}


def test_list_filter_by_period(client_admin, student_a):
    client_admin.post(
        "/api/fees/adjustments", json=_payload(student_a.id, period="114-1")
    )
    client_admin.post(
        "/api/fees/adjustments", json=_payload(student_a.id, period="114-2")
    )
    r = client_admin.get("/api/fees/adjustments?period=114-2")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["period"] == "114-2"


def test_update_adjustment(client_admin, student_a):
    create = client_admin.post("/api/fees/adjustments", json=_payload(student_a.id))
    adj_id = create.json()["id"]
    r = client_admin.put(
        f"/api/fees/adjustments/{adj_id}",
        json={"amount": 2000, "reason": "更新後"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["amount"] == 2000
    assert body["reason"] == "更新後"
    # 未變動欄位保留
    assert body["adjustment_type"] == "sibling_discount"


def test_update_nonexistent_404(client_admin):
    r = client_admin.put("/api/fees/adjustments/99999", json={"amount": 100})
    assert r.status_code == 404


def test_delete_adjustment(client_admin, student_a):
    create = client_admin.post("/api/fees/adjustments", json=_payload(student_a.id))
    adj_id = create.json()["id"]
    r = client_admin.delete(f"/api/fees/adjustments/{adj_id}")
    assert r.status_code == 200
    # 確認真的刪除
    r2 = client_admin.get(f"/api/fees/adjustments?student_id={student_a.id}")
    assert r2.json()["total"] == 0


def test_delete_nonexistent_404(client_admin):
    r = client_admin.delete("/api/fees/adjustments/99999")
    assert r.status_code == 404


def test_same_student_period_type_multiple_allowed(client_admin, student_a):
    """同學生同學期同 type 可有多筆（不加 UNIQUE，允許多次扣款/折抵）。"""
    r1 = client_admin.post("/api/fees/adjustments", json=_payload(student_a.id))
    r2 = client_admin.post("/api/fees/adjustments", json=_payload(student_a.id))
    assert r1.status_code == 200
    assert r2.status_code == 200
    r3 = client_admin.get(f"/api/fees/adjustments?student_id={student_a.id}")
    assert r3.json()["total"] == 2
