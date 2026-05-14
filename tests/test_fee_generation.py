"""依範本批量產生 FeeRecord 測試"""

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
from models.classroom import (
    ClassGrade,
    Classroom,
    Student,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_ENROLLED,
    LIFECYCLE_ON_LEAVE,
    LIFECYCLE_WITHDRAWN,
)
from models.database import User
from models.fees import FeeTemplate, StudentFeeRecord
from utils.auth import hash_password

# ---------------------------------------------------------------------------
# Fixtures: app + DB + admin client (engine swap pattern, 同 test_fee_templates)
# ---------------------------------------------------------------------------


@pytest.fixture
def _backend(tmp_path):
    """建立檔案型 SQLite engine，swap global _engine/_SessionFactory，
    讓 api/fees 透過 session_scope() 看到同一份資料。"""
    db_path = tmp_path / "fee_gen.sqlite"
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
            username="gen_admin",
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
        json={"username": "gen_admin", "password": "Temp123456"},
    )
    assert r.status_code == 200, f"admin login failed: {r.text}"
    yield client
    client.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def setup_school(session):
    """3 年級各 1 班,大班 2 學生(active+on_leave)、中班 1 學生(enrolled)、
    小班 1 學生(withdrawn)。"""
    grades = {}
    for name, order in [("大班", 3), ("中班", 2), ("小班", 1), ("幼幼班", 0)]:
        g = ClassGrade(name=name, sort_order=order, is_active=True)
        session.add(g)
        grades[name] = g
    session.flush()

    classrooms = {}
    for grade_name, cls_name in [
        ("大班", "大班 A"),
        ("中班", "中班 A"),
        ("小班", "小班 A"),
    ]:
        c = Classroom(
            name=cls_name,
            school_year=114,
            semester=1,
            grade_id=grades[grade_name].id,
            is_active=True,
        )
        session.add(c)
        classrooms[grade_name] = c
    session.flush()

    students = []

    def _make(name, classroom, lc):
        s = Student(
            student_id=f"S{len(students)+1:03d}",
            name=name,
            classroom_id=classroom.id,
            lifecycle_status=lc,
            is_active=True,
        )
        session.add(s)
        students.append(s)
        return s

    _make("大A1", classrooms["大班"], LIFECYCLE_ACTIVE)
    _make("大A2", classrooms["大班"], LIFECYCLE_ON_LEAVE)  # 跳過
    _make("中A1", classrooms["中班"], LIFECYCLE_ENROLLED)
    _make("小A1", classrooms["小班"], LIFECYCLE_WITHDRAWN)  # 跳過
    session.commit()
    return {"grades": grades, "classrooms": classrooms, "students": students}


def _make_template(session, grade, fee_type, amount, **kw):
    kw.setdefault("semester", 1)
    t = FeeTemplate(
        grade_id=grade.id,
        school_year=114,
        fee_type=fee_type,
        name=f"114-1 {fee_type}",
        amount=amount,
        **kw,
    )
    session.add(t)
    session.flush()
    return t


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_generate_registration_creates_for_active_and_enrolled_only(
    client_admin, setup_school, session
):
    grades = setup_school["grades"]
    _make_template(session, grades["大班"], "registration", 19000)
    _make_template(session, grades["中班"], "registration", 19000)
    _make_template(session, grades["小班"], "registration", 19000)
    session.commit()

    r = client_admin.post(
        "/api/fees/generate",
        json={
            "school_year": 114,
            "semester": 1,
            "fee_types": ["registration"],
            "dry_run": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # 大班A1 + 中班A1 = 2 筆;on_leave 與 withdrawn 跳過
    assert body["created"] == 2
    assert body["skipped"] == 0

    records = (
        session.query(StudentFeeRecord)
        .filter(StudentFeeRecord.fee_type == "registration")
        .all()
    )
    assert len(records) == 2
    assert all(r.amount_due == 19000 for r in records)


def test_generate_dry_run_does_not_persist(client_admin, setup_school, session):
    grades = setup_school["grades"]
    _make_template(session, grades["大班"], "registration", 19000)
    session.commit()

    r = client_admin.post(
        "/api/fees/generate",
        json={
            "school_year": 114,
            "semester": 1,
            "fee_types": ["registration"],
            "dry_run": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["created"] == 1
    assert session.query(StudentFeeRecord).count() == 0


def test_generate_idempotent(client_admin, setup_school, session):
    grades = setup_school["grades"]
    _make_template(session, grades["大班"], "registration", 19000)
    _make_template(session, grades["中班"], "registration", 19000)
    session.commit()

    # 第一次
    client_admin.post(
        "/api/fees/generate",
        json={
            "school_year": 114,
            "semester": 1,
            "fee_types": ["registration"],
            "dry_run": False,
        },
    )
    # 第二次:全跳過
    r = client_admin.post(
        "/api/fees/generate",
        json={
            "school_year": 114,
            "semester": 1,
            "fee_types": ["registration"],
            "dry_run": False,
        },
    )
    body = r.json()
    assert body["created"] == 0
    assert body["skipped"] == 2


def test_generate_monthly_expands_six_months(client_admin, setup_school, session):
    """上學期 monthly 範本展開 8/9/10/11/12/1 月 6 張單據。"""
    grades = setup_school["grades"]
    _make_template(
        session,
        grades["大班"],
        "monthly",
        13000,
        breakdown={"tuition": 8500, "meal": 3000, "transport": 1500},
    )
    session.commit()

    r = client_admin.post(
        "/api/fees/generate",
        json={
            "school_year": 114,
            "semester": 1,
            "fee_types": ["monthly"],
            "dry_run": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # 大班 active 1 學生 × 6 月 = 6 張
    assert body["created"] == 6

    months = sorted(
        rec.target_month
        for rec in session.query(StudentFeeRecord)
        .filter(StudentFeeRecord.fee_type == "monthly")
        .all()
    )
    # 民國 114 = 西元 2025; 上學期: 8/9/10/11/12 月與隔年 1 月
    expected = sorted([f"2025-{m:02d}" for m in range(8, 13)] + ["2026-01"])
    assert months == expected


def test_generate_monthly_lower_semester(client_admin, setup_school, session):
    """下學期 monthly 展開 2/3/4/5/6/7 月。"""
    grades = setup_school["grades"]
    # 重新建一個下學期班級
    c = Classroom(
        name="大班 A",
        school_year=114,
        semester=2,
        grade_id=grades["大班"].id,
        is_active=True,
    )
    session.add(c)
    session.flush()
    s = Student(
        student_id="S100",
        name="x",
        classroom_id=c.id,
        lifecycle_status=LIFECYCLE_ACTIVE,
        is_active=True,
    )
    session.add(s)

    _make_template(
        session,
        grades["大班"],
        "monthly",
        13000,
        semester=2,
        breakdown={"tuition": 8500, "meal": 3000, "transport": 1500},
    )
    session.commit()

    r = client_admin.post(
        "/api/fees/generate",
        json={
            "school_year": 114,
            "semester": 2,
            "fee_types": ["monthly"],
            "dry_run": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    months = sorted(
        rec.target_month
        for rec in session.query(StudentFeeRecord)
        .filter(StudentFeeRecord.fee_type == "monthly")
        .all()
    )
    expected = sorted([f"2026-{m:02d}" for m in range(2, 8)])
    assert months == expected
    assert body["created"] == len(expected)


def test_generate_no_template_for_grade_skipped(client_admin, setup_school, session):
    """中班 active 學生但沒有中班範本 → 不產生(只大班 A1 產生)。"""
    grades = setup_school["grades"]
    _make_template(session, grades["大班"], "registration", 19000)
    # 中班無範本
    session.commit()

    r = client_admin.post(
        "/api/fees/generate",
        json={
            "school_year": 114,
            "semester": 1,
            "fee_types": ["registration"],
            "dry_run": False,
        },
    )
    body = r.json()
    assert body["created"] == 1  # 只有大班 A1
