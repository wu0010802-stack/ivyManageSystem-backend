"""tests/test_employees_class_history.py — 員工班級歷程 service + endpoint 測試。"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.employees import router as employees_router
from models.base import Base
from models.database import (
    Classroom,
    Employee,
    Student,
    User,
)
from models.classroom import ClassGrade
from models.gov_moe import MonthlyEnrollmentSnapshot
from services.employee_class_history import _term_headcounts, build_class_history
from utils.academic import term_bounds
from utils.auth import hash_password


@pytest.fixture
def db():
    """SQLite in-memory session（swap 全域 engine）。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    s = session_factory()
    try:
        yield s, session_factory
    finally:
        s.close()
        base_module._engine = old_engine
        base_module._SessionFactory = old_sf
        engine.dispose()


def _mk_classroom(
    s,
    *,
    name,
    school_year,
    semester,
    head=None,
    assistant=None,
    art=None,
    grade_id=None,
):
    c = Classroom(
        name=name,
        school_year=school_year,
        semester=semester,
        head_teacher_id=head,
        assistant_teacher_id=assistant,
        art_teacher_id=art,
        grade_id=grade_id,
    )
    s.add(c)
    s.flush()
    return c


def test_term_headcounts_past_reads_snapshot(db):
    """過去學期：期初讀開學月快照、期末讀期末月快照、跨 age_group 加總。"""
    s, _ = db
    c = _mk_classroom(s, name="葡萄班", school_year=113, semester=2, head=1)
    start_date, end_date = term_bounds(113, 2)
    s.add_all(
        [
            MonthlyEnrollmentSnapshot(
                year=start_date.year,
                month=start_date.month,
                classroom_id=c.id,
                age_group="3-4",
                total_count=10,
            ),
            MonthlyEnrollmentSnapshot(
                year=start_date.year,
                month=start_date.month,
                classroom_id=c.id,
                age_group="4-5",
                total_count=12,
            ),
            MonthlyEnrollmentSnapshot(
                year=end_date.year,
                month=end_date.month,
                classroom_id=c.id,
                age_group="3-4",
                total_count=20,
            ),
        ]
    )
    s.commit()
    start, end, is_live = _term_headcounts(s, c.id, 113, 2, is_current=False)
    assert start == 22
    assert end == 20
    assert is_live is False


def test_term_headcounts_no_snapshot_returns_none(db):
    """過去學期無快照 → start/end 皆 None。"""
    s, _ = db
    c = _mk_classroom(s, name="無料班", school_year=112, semester=1, head=1)
    s.commit()
    start, end, is_live = _term_headcounts(s, c.id, 112, 1, is_current=False)
    assert start is None
    assert end is None
    assert is_live is False


def test_term_headcounts_current_uses_live_end(db):
    """當前學期：期末=即時在籍數、is_live=True。"""
    s, _ = db
    c = _mk_classroom(s, name="蘋果班", school_year=114, semester=2, head=1)
    s.add_all(
        [
            Student(
                student_id="S1",
                name="生一",
                classroom_id=c.id,
                enrollment_date=date(2024, 8, 1),
            ),
            Student(
                student_id="S2",
                name="生二",
                classroom_id=c.id,
                enrollment_date=date(2024, 8, 1),
            ),
        ]
    )
    s.commit()
    start, end, is_live = _term_headcounts(s, c.id, 114, 2, is_current=True)
    assert start is None
    assert end == 2
    assert is_live is True


def test_build_class_history_spine_excludes_art_and_sorts(db):
    """主幹：含 head/assistant 班、排除純 art 班；依學期由新到舊。"""
    s, _ = db
    teacher = Employee(employee_id="T001", name="王老師", employee_type="regular")
    other = Employee(employee_id="T002", name="李助教", employee_type="regular")
    s.add_all([teacher, other])
    s.flush()
    _mk_classroom(
        s,
        name="蘋果班",
        school_year=114,
        semester=2,
        head=teacher.id,
        assistant=other.id,
    )
    _mk_classroom(
        s,
        name="葡萄班",
        school_year=113,
        semester=2,
        head=other.id,
        assistant=teacher.id,
    )
    _mk_classroom(
        s, name="音樂班", school_year=114, semester=1, head=other.id, art=teacher.id
    )
    s.commit()

    rows = build_class_history(s, teacher.id)
    assert [(r["school_year"], r["semester"], r["role"]) for r in rows] == [
        (114, 2, "head"),
        (113, 2, "assistant"),
    ]


def test_build_class_history_co_teachers(db):
    """同班搭檔：含才藝、排除自己、有姓名。"""
    s, _ = db
    me = Employee(employee_id="T010", name="我", employee_type="regular")
    asst = Employee(employee_id="T011", name="助教甲", employee_type="regular")
    art = Employee(employee_id="T012", name="才藝乙", employee_type="regular")
    s.add_all([me, asst, art])
    s.flush()
    _mk_classroom(
        s,
        name="蘋果班",
        school_year=114,
        semester=2,
        head=me.id,
        assistant=asst.id,
        art=art.id,
    )
    s.commit()

    rows = build_class_history(s, me.id)
    assert len(rows) == 1
    cos = {(c["role"], c["name"]) for c in rows[0]["co_teachers"]}
    assert cos == {("assistant", "助教甲"), ("art", "才藝乙")}
    assert all(c["employee_id"] != me.id for c in rows[0]["co_teachers"])


def test_build_class_history_net_change_only_when_both_present(db):
    """net_change 僅兩數皆有才算。"""
    s, _ = db
    me = Employee(employee_id="T020", name="我", employee_type="regular")
    s.add(me)
    s.flush()
    c = _mk_classroom(s, name="葡萄班", school_year=113, semester=2, head=me.id)
    start_date, end_date = term_bounds(113, 2)
    s.add_all(
        [
            MonthlyEnrollmentSnapshot(
                year=start_date.year,
                month=start_date.month,
                classroom_id=c.id,
                age_group="3-4",
                total_count=22,
            ),
            MonthlyEnrollmentSnapshot(
                year=end_date.year,
                month=end_date.month,
                classroom_id=c.id,
                age_group="3-4",
                total_count=20,
            ),
        ]
    )
    s.commit()
    rows = build_class_history(s, me.id)
    assert rows[0]["start_count"] == 22
    assert rows[0]["end_count"] == 20
    assert rows[0]["net_change"] == -2


def test_build_class_history_empty(db):
    """沒帶過任何班 → 空陣列。"""
    s, _ = db
    me = Employee(employee_id="T030", name="閒人", employee_type="regular")
    s.add(me)
    s.commit()
    assert build_class_history(s, me.id) == []


def test_build_class_history_head_priority_when_same_person_both_roles(db):
    """同一人同時是某班 head 與 assistant → 只出一列、role=head、不在 co_teachers。"""
    s, _ = db
    me = Employee(employee_id="T010", name="我", employee_type="regular")
    s.add(me)
    s.flush()
    _mk_classroom(
        s, name="蘋果班", school_year=114, semester=2, head=me.id, assistant=me.id
    )
    s.commit()
    rows = build_class_history(s, me.id)
    assert len(rows) == 1
    assert rows[0]["role"] == "head"
    assert all(c["employee_id"] != me.id for c in rows[0]["co_teachers"])


def test_build_class_history_net_change_none_when_count_missing(db):
    """無快照（過去學期）→ start/end 皆 None → net_change None。"""
    s, _ = db
    me = Employee(employee_id="T011", name="我", employee_type="regular")
    s.add(me)
    s.flush()
    _mk_classroom(s, name="無料班", school_year=112, semester=1, head=me.id)
    s.commit()
    rows = build_class_history(s, me.id)
    assert len(rows) == 1
    assert rows[0]["start_count"] is None
    assert rows[0]["end_count"] is None
    assert rows[0]["net_change"] is None


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    app = FastAPI()
    from utils.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(employees_router)
    s = session_factory()
    with TestClient(app) as c:
        yield c, s, session_factory
    s.close()
    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _login_admin(client, sf):
    with sf() as s:
        s.add(
            User(
                username="admin",
                password_hash=hash_password("Temp123456"),
                role="admin",
                permission_names=["EMPLOYEES_READ"],
                employee_id=None,
                is_active=True,
                must_change_password=False,
            )
        )
        s.commit()
    r = client.post(
        "/api/auth/login", json={"username": "admin", "password": "Temp123456"}
    )
    assert r.status_code == 200, r.json()


def test_class_history_endpoint_returns_rows(client):
    c, s, sf = client
    me = Employee(employee_id="T100", name="王老師", employee_type="regular")
    s.add(me)
    s.flush()
    _mk_classroom(s, name="蘋果班", school_year=114, semester=2, head=me.id)
    s.commit()
    _login_admin(c, sf)

    r = c.get(f"/api/employees/{me.id}/class-history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["role"] == "head"
    assert body["rows"][0]["classroom_name"] == "蘋果班"


def test_class_history_endpoint_requires_permission(client):
    c, s, sf = client
    me = Employee(employee_id="T101", name="王老師", employee_type="regular")
    s.add(me)
    s.commit()
    r = c.get(f"/api/employees/{me.id}/class-history")
    assert r.status_code in (401, 403)
