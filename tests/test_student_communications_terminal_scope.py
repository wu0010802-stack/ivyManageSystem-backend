"""SEC-003：list_communications 的 classroom_id 分支須排除終態學生（班級範圍 caller）。

終態（畢業/退學/轉出）轉換刻意不清 Student.classroom_id，學生仍「在」班。原
classroom_id 分支只 filter(classroom_id==) 無 lifecycle 過濾，使班級範圍 staff 可讀到
已離園兒童的家長溝通紀錄（每班 PII 洩漏）。student_id 分支走 assert_student_access、
else 分支走 student_ids_in_scope 皆已排除終態，唯獨 classroom_id 分支漏 R5-1 修補。
管理角色（unrestricted）仍應看得到終態學生紀錄（供事後查歷史）。
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
from api.student_communications import router as comm_router
from models.classroom import LIFECYCLE_ACTIVE, LIFECYCLE_WITHDRAWN
from models.database import Base, Classroom, Employee, Student, User
from models.student_log import ParentCommunicationLog
from utils.auth import hash_password


@pytest.fixture
def comm_app(tmp_path):
    db_path = tmp_path / "comm-terminal.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(comm_router)
    with TestClient(app) as client:
        yield client, sf
    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_e
    base_module._SessionFactory = old_sf
    engine.dispose()


def _login(client, username, password="Pass1234"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _seed(session) -> dict:
    emp = Employee(
        employee_id="ET01",
        name="班導",
        base_salary=32000,
        is_active=True,
        hire_date=date(2024, 1, 1),
    )
    session.add(emp)
    session.flush()
    cls = Classroom(
        name="向日葵班",
        school_year=2025,
        semester=1,
        is_active=True,
        head_teacher_id=emp.id,
    )
    session.add(cls)
    session.flush()
    active = Student(
        student_id="ACT01",
        name="在學生",
        classroom_id=cls.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    terminal = Student(
        student_id="TRM01",
        name="已離園生",
        classroom_id=cls.id,  # 終態轉換刻意不清 classroom_id → 仍「在」班
        is_active=False,
        lifecycle_status=LIFECYCLE_WITHDRAWN,
    )
    session.add_all([active, terminal])
    session.flush()
    session.add_all(
        [
            ParentCommunicationLog(
                student_id=active.id,
                communication_date=date(2026, 4, 10),
                communication_type="電話",
                topic="在學溝通",
                content="x",
            ),
            ParentCommunicationLog(
                student_id=terminal.id,
                communication_date=date(2026, 4, 11),
                communication_type="電話",
                topic="離園生機密溝通",
                content="y",
            ),
        ]
    )
    teacher = User(
        username="t_homeroom",
        password_hash=hash_password("Pass1234"),
        role="staff",  # 班級範圍（非 unrestricted）
        permission_names=["STUDENTS_READ"],
        employee_id=emp.id,
        is_active=True,
        must_change_password=False,
    )
    admin = User(
        username="t_admin",
        password_hash=hash_password("Pass1234"),
        role="admin",
        permission_names=["STUDENTS_READ"],
        is_active=True,
        must_change_password=False,
    )
    session.add_all([teacher, admin])
    session.commit()
    return {"cls": cls.id, "active": active.id, "terminal": terminal.id}


def test_classroom_branch_hides_terminal_student_from_scoped_teacher(comm_app):
    client, sf = comm_app
    with sf() as s:
        ids = _seed(s)
    assert _login(client, "t_homeroom").status_code == 200
    r = client.get("/api/students/communications", params={"classroom_id": ids["cls"]})
    assert r.status_code == 200, r.text
    seen = {it["student_id"] for it in r.json()["items"]}
    assert ids["active"] in seen, "在學生紀錄應可見"
    assert (
        ids["terminal"] not in seen
    ), "班級範圍 staff 不應看到終態學生的家長溝通紀錄（SEC-003）"


def test_classroom_branch_admin_still_sees_terminal(comm_app):
    client, sf = comm_app
    with sf() as s:
        ids = _seed(s)
    assert _login(client, "t_admin").status_code == 200
    r = client.get("/api/students/communications", params={"classroom_id": ids["cls"]})
    assert r.status_code == 200, r.text
    seen = {it["student_id"] for it in r.json()["items"]}
    assert (
        ids["active"] in seen and ids["terminal"] in seen
    ), "管理角色仍可查終態學生紀錄（供事後查歷史）"
