"""驗證 PUT /api/students/{id} 在學生 lifecycle 為終態（畢業/退學/轉出）時拒絕寫入。

威脅：4/30 commit 58a62a60 已加 sub-resource 與家長端終態守衛，但學生主資料 PUT
未擋。畢業學生仍可被改家長電話/班級/緊急聯絡人，造成稽核斷鏈與資料一致性問題。

Refs: 資安掃描 2026-05-07 P0。
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
from api.students import router as students_router
from models.classroom import (
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
)
from models.database import Base, Classroom, Student, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def students_client(tmp_path):
    db_path = tmp_path / "students_terminal_guard.sqlite"
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
    app.include_router(students_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_admin(session, permission_names):
    u = User(
        username="admin_t",
        password_hash=hash_password("Passw0rd!"),
        role="admin",
        permission_names=permission_names,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client):
    return client.post(
        "/api/auth/login",
        json={"username": "admin_t", "password": "Passw0rd!"},
    )


def _seed_student(session, lifecycle_status: str, name: str = "畢業生"):
    cls = Classroom(name="大班A", is_active=True)
    session.add(cls)
    session.flush()
    s = Student(
        student_id="S999",
        name=name,
        classroom_id=cls.id,
        is_active=True,
        parent_name="家長",
        parent_phone="0900-111-222",
        lifecycle_status=lifecycle_status,
    )
    session.add(s)
    session.flush()
    return s


WRITE_PERMS = ["STUDENTS_WRITE", "STUDENTS_READ"]


class TestPutStudentTerminalGuard:
    @pytest.mark.parametrize(
        "terminal_status",
        [LIFECYCLE_GRADUATED, LIFECYCLE_WITHDRAWN, LIFECYCLE_TRANSFERRED],
    )
    def test_put_blocked_for_terminal_lifecycle(self, students_client, terminal_status):
        client, sf = students_client
        with sf() as s:
            _create_admin(s, permission_names=WRITE_PERMS)
            student = _seed_student(s, lifecycle_status=terminal_status)
            s.commit()
            sid = student.id
            original_phone = student.parent_phone

        assert _login(client).status_code == 200
        res = client.put(
            f"/api/students/{sid}",
            json={"parent_phone": "0900-999-888"},
        )
        assert res.status_code == 403, res.text
        assert "離校" in res.json().get("detail", "")

        # 確認 DB 沒被改
        with sf() as s:
            still = s.query(Student).filter(Student.id == sid).first()
            assert still.parent_phone == original_phone

    def test_put_allowed_for_enrolled(self, students_client):
        client, sf = students_client
        with sf() as s:
            _create_admin(s, permission_names=WRITE_PERMS)
            student = _seed_student(s, lifecycle_status="enrolled")
            s.commit()
            sid = student.id

        assert _login(client).status_code == 200
        res = client.put(
            f"/api/students/{sid}",
            json={"parent_phone": "0900-999-888"},
        )
        assert res.status_code == 200, res.text
