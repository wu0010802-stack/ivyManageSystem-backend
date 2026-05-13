"""教師端 GET /api/portal/students/measurements-latest 測試。

涵蓋：
- RBAC：教師只回自己班學生（含 head/assistant/art teacher 三角色）
- 終態學生排除（graduated/withdrawn/transferred）
- 從未量測的學生 → last_measurement = None
- 同日多筆 → 取 id 最大者
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.portal import router as portal_router
from models.classroom import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
)
from models.database import (
    Base,
    Classroom,
    Employee,
    Student,
    StudentMeasurement,
    User,
)
from utils.auth import create_access_token
from utils.permissions import Permission


@pytest.fixture
def client_and_session(tmp_path):
    import models.base as base_module

    db_path = tmp_path / "ml.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf

    app = FastAPI()
    app.include_router(portal_router)
    with TestClient(app) as client:
        sess = sf()
        yield client, sess
        sess.close()

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed_teacher(
    sess, classroom: Classroom, role: str = "head"
) -> tuple[Employee, User, str]:
    """建立一位教師（attach 到 classroom 指定角色），回 (employee, user, jwt)。"""
    emp = Employee(employee_id="T_ML_001", name="王老師", is_active=True)
    sess.add(emp)
    sess.flush()
    if role == "head":
        classroom.head_teacher_id = emp.id
    elif role == "assistant":
        classroom.assistant_teacher_id = emp.id
    elif role == "art":
        classroom.art_teacher_id = emp.id
    sess.flush()
    user = User(
        username="wang",
        password_hash="x",
        role="teacher",
        employee_id=emp.id,
        permissions=int(Permission.PORTFOLIO_READ | Permission.PORTFOLIO_WRITE),
        is_active=True,
        token_version=0,
    )
    sess.add(user)
    sess.flush()
    token = create_access_token(
        {
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
            "employee_id": emp.id,
            "permissions": user.permissions,
            "token_version": 0,
        }
    )
    return emp, user, token


def test_returns_own_class_students_only(client_and_session):
    client, sess = client_and_session
    # 班 A 由 teacher A 帶；班 B 由 teacher B 帶
    classroom_a = Classroom(name="A", is_active=True)
    classroom_b = Classroom(name="B", is_active=True)
    sess.add_all([classroom_a, classroom_b])
    sess.flush()
    s_a = Student(
        student_id="A1",
        name="小A",
        classroom_id=classroom_a.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    s_b = Student(
        student_id="B1",
        name="小B",
        classroom_id=classroom_b.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    sess.add_all([s_a, s_b])
    sess.flush()
    _, _, token = _seed_teacher(sess, classroom_a, role="head")
    sess.commit()

    resp = client.get(
        "/api/portal/students/measurements-latest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    ids = {row["student_id"] for row in data}
    assert ids == {s_a.id}  # 不含 s_b


def test_excludes_terminal_students(client_and_session):
    client, sess = client_and_session
    classroom = Classroom(name="A", is_active=True)
    sess.add(classroom)
    sess.flush()
    active = Student(
        student_id="A1",
        name="在學",
        classroom_id=classroom.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    grad = Student(
        student_id="A2",
        name="畢業",
        classroom_id=classroom.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_GRADUATED,
    )
    withdrawn = Student(
        student_id="A3",
        name="退學",
        classroom_id=classroom.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_WITHDRAWN,
    )
    transferred = Student(
        student_id="A4",
        name="轉出",
        classroom_id=classroom.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_TRANSFERRED,
    )
    sess.add_all([active, grad, withdrawn, transferred])
    sess.flush()
    _, _, token = _seed_teacher(sess, classroom)
    sess.commit()

    resp = client.get(
        "/api/portal/students/measurements-latest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    ids = {row["student_id"] for row in resp.json()}
    assert ids == {active.id}


def test_last_measurement_null_when_no_record(client_and_session):
    client, sess = client_and_session
    classroom = Classroom(name="A", is_active=True)
    sess.add(classroom)
    sess.flush()
    student = Student(
        student_id="A1",
        name="小A",
        classroom_id=classroom.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    sess.add(student)
    sess.flush()
    _, _, token = _seed_teacher(sess, classroom)
    sess.commit()

    resp = client.get(
        "/api/portal/students/measurements-latest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    row = resp.json()[0]
    assert row["student_id"] == student.id
    assert row["last_measurement"] is None


def test_returns_403_without_portfolio_read(client_and_session):
    """User without PORTFOLIO_READ should get 403."""
    client, sess = client_and_session
    # 用 _seed_teacher 建好 emp+user，再覆寫 permissions=0、重簽 token
    classroom = Classroom(name="A", is_active=True)
    sess.add(classroom)
    sess.flush()
    emp, user, _ = _seed_teacher(sess, classroom)
    user.permissions = 0
    sess.flush()
    token = create_access_token(
        {
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
            "employee_id": emp.id,
            "permissions": 0,
        }
    )
    sess.commit()

    resp = client.get(
        "/api/portal/students/measurements-latest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_returns_latest_by_date_then_id(client_and_session):
    client, sess = client_and_session
    classroom = Classroom(name="A", is_active=True)
    sess.add(classroom)
    sess.flush()
    student = Student(
        student_id="A1",
        name="小A",
        classroom_id=classroom.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    sess.add(student)
    sess.flush()
    # 同日兩筆 → 應取 id 較大者；不同日 → 取較新日期
    m_old = StudentMeasurement(
        student_id=student.id, measured_on=date(2026, 4, 1), height_cm=100
    )
    m_same_day_first = StudentMeasurement(
        student_id=student.id, measured_on=date(2026, 5, 1), height_cm=101
    )
    m_same_day_later = StudentMeasurement(
        student_id=student.id, measured_on=date(2026, 5, 1), height_cm=102
    )
    sess.add_all([m_old, m_same_day_first, m_same_day_later])
    sess.flush()
    _, _, token = _seed_teacher(sess, classroom)
    sess.commit()

    resp = client.get(
        "/api/portal/students/measurements-latest",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    row = resp.json()[0]
    assert row["last_measurement"]["measured_on"] == "2026-05-01"
    assert row["last_measurement"]["height_cm"] == "102.00"
