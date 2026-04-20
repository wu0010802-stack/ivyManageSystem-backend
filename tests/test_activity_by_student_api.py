"""才藝報名「按學生查詢」端點整合測試。

測試端點：GET /api/activity/registrations?student_id=X
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
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    Student,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "activity-by-student.sqlite"
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

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _add_user(
    session,
    username="admin",
    password="TempPass123",
    perms=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
):
    u = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=perms,
        is_active=True,
    )
    session.add(u)
    session.flush()
    return u


def _login(client, username="admin", password="TempPass123"):
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200
    return r


def _seed(session):
    """建立：1 班、2 學生、2 課程、跨學期報名。回傳 id 字典。"""
    _add_user(session)
    classroom = Classroom(name="大象班", is_active=True)
    session.add(classroom)
    session.flush()

    target = Student(
        student_id="S001",
        name="小明",
        classroom_id=classroom.id,
        is_active=True,
    )
    other = Student(
        student_id="S002",
        name="小華",
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add_all([target, other])
    session.flush()

    course_a = ActivityCourse(name="美術", price=1000, capacity=30, is_active=True)
    course_b = ActivityCourse(name="圍棋", price=1200, capacity=30, is_active=True)
    session.add_all([course_a, course_b])
    session.flush()

    reg_up = ActivityRegistration(
        student_name="小明",
        birthday="2020-01-01",
        class_name="大象班",
        classroom_id=classroom.id,
        student_id=target.id,
        school_year=114,
        semester=1,
        parent_phone="0912",
        is_active=True,
    )
    reg_down = ActivityRegistration(
        student_name="小明",
        birthday="2020-01-01",
        class_name="大象班",
        classroom_id=classroom.id,
        student_id=target.id,
        school_year=114,
        semester=2,
        parent_phone="0912",
        is_active=True,
    )
    reg_other = ActivityRegistration(
        student_name="小華",
        birthday="2020-02-02",
        class_name="大象班",
        classroom_id=classroom.id,
        student_id=other.id,
        school_year=114,
        semester=1,
        parent_phone="0922",
        is_active=True,
    )
    session.add_all([reg_up, reg_down, reg_other])
    session.commit()
    return {"target": target.id, "other": other.id}


class TestRegistrationsByStudent:
    def test_registrations_student_id_returns_all_terms(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
        _login(client)

        res = client.get(
            "/api/activity/registrations",
            params={"student_id": ids["target"], "limit": 50},
        )
        assert res.status_code == 200
        data = res.json()
        # 該學生上下學期各一筆，應回 2 筆（不因預設學期過濾遺失）
        assert data["total"] == 2
        semesters = sorted([item["semester"] for item in data["items"]])
        assert semesters == [1, 2]
        names = {item["student_name"] for item in data["items"]}
        assert names == {"小明"}

    def test_registrations_excludes_other_students(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
        _login(client)

        res = client.get(
            "/api/activity/registrations", params={"student_id": ids["other"]}
        )
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 1
        assert items[0]["student_name"] == "小華"
