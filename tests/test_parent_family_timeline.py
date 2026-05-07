"""家校 timeline endpoint。"""

import os
import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import parent_router as parent_portal_router
from api.parent_portal.family import _timeline_cache
from models.classroom import StudentAttendance
from models.database import (
    Base,
    Classroom,
    Guardian,
    Student,
    User,
)
from utils.auth import create_access_token


@pytest.fixture
def parent_family_client(tmp_path):
    db_path = tmp_path / "parent-family.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)
    # 模組級 TTLCache 會跨測試殘留；每個 fixture 清空一次。
    _timeline_cache.clear()

    app = FastAPI()
    app.include_router(parent_portal_router)
    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _make_parent_with_child(
    session, *, line_user_id: str, student_name: str, classroom_name: str = "向日葵班"
):
    user = User(
        username=f"parent_line_{line_user_id}",
        password_hash="x",
        role="parent",
        line_user_id=line_user_id,
        is_active=True,
    )
    session.add(user)
    session.flush()
    classroom = (
        session.query(Classroom).filter(Classroom.name == classroom_name).first()
    )
    if not classroom:
        classroom = Classroom(name=classroom_name, is_active=True)
        session.add(classroom)
        session.flush()
    student = Student(
        student_id=f"S{user.id:04d}",
        name=student_name,
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()
    guardian = Guardian(
        user_id=user.id,
        student_id=student.id,
        relation="父",
        is_primary=True,
        name="家長",
    )
    session.add(guardian)
    session.commit()
    return user, student


def _auth_header(user_id: int) -> dict:
    token = create_access_token({"user_id": user_id, "role": "parent"})
    return {"Authorization": f"Bearer {token}"}


def test_timeline_403_for_non_owned_student(parent_family_client):
    """傳非自己孩子 student_id → 403。"""
    client, factory = parent_family_client
    session = factory()
    user_a, _student_a = _make_parent_with_child(
        session, line_user_id="UA", student_name="A 童", classroom_name="A班"
    )
    _user_b, student_b = _make_parent_with_child(
        session, line_user_id="UB", student_name="B 童", classroom_name="B班"
    )
    user_a_id = user_a.id
    student_b_id = student_b.id
    session.close()

    resp = client.get(
        f"/api/parent/family/timeline?student_id={student_b_id}",
        headers=_auth_header(user_a_id),
    )
    assert resp.status_code == 403


def test_timeline_empty_when_no_data(parent_family_client):
    """子女資料但無任何 attendance/announcement/etc → 空 list。"""
    client, factory = parent_family_client
    session = factory()
    user, student = _make_parent_with_child(
        session, line_user_id="U1", student_name="小明"
    )
    user_id = user.id
    student_id = student.id
    session.close()

    resp = client.get(
        f"/api/parent/family/timeline?student_id={student_id}",
        headers=_auth_header(user_id),
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_timeline_includes_today_attendance(parent_family_client):
    """今日 attendance 應出現在 timeline。"""
    client, factory = parent_family_client
    session = factory()
    user, student = _make_parent_with_child(
        session, line_user_id="U1", student_name="小明"
    )
    today = date.today()
    session.add(
        StudentAttendance(
            student_id=student.id,
            date=today,
            status="出席",
        )
    )
    session.commit()
    user_id = user.id
    student_id = student.id
    session.close()

    resp = client.get(
        f"/api/parent/family/timeline?student_id={student_id}",
        headers=_auth_header(user_id),
    )
    assert resp.status_code == 200
    items = resp.json()
    assert any(it["kind"] == "attendance" for it in items)
    att = next(it for it in items if it["kind"] == "attendance")
    assert att["href"] == "/attendance"
    assert att["is_pending"] is False


def test_timeline_limit_param(parent_family_client):
    """limit 參數限制回傳筆數。"""
    client, factory = parent_family_client
    session = factory()
    user, student = _make_parent_with_child(
        session, line_user_id="U1", student_name="小明"
    )
    # 建 10 天 attendance
    for i in range(10):
        session.add(
            StudentAttendance(
                student_id=student.id,
                date=date.today() - timedelta(days=i),
                status="出席",
            )
        )
    session.commit()
    user_id = user.id
    student_id = student.id
    session.close()

    resp = client.get(
        f"/api/parent/family/timeline?student_id={student_id}&limit=3",
        headers=_auth_header(user_id),
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 3
