"""班級 CRUD API 回歸測試。"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import router as auth_router
from api.classrooms import router as classrooms_router
from api.auth import _account_failures, _ip_attempts
from models.database import Base, ClassGrade, Classroom, Employee, Student, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "classrooms-api.sqlite"
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
    app.include_router(classrooms_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, username: str, password: str = "TempPass123") -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=Permission.CLASSROOMS_READ | Permission.CLASSROOMS_WRITE,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _create_teacher(session, employee_id: str, name: str) -> Employee:
    teacher = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=32000,
        is_active=True,
    )
    session.add(teacher)
    session.flush()
    return teacher


def _login(client: TestClient, username: str, password: str = "TempPass123"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


class TestClassroomsApi:
    def test_create_rejects_duplicate_teacher_roles(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "classroom_admin")
            grade = ClassGrade(name="大班", is_active=True)
            teacher = _create_teacher(session, "T001", "王老師")
            session.add(grade)
            session.commit()
            grade_id = grade.id
            teacher_id = teacher.id

        login_res = _login(client, "classroom_admin")
        assert login_res.status_code == 200

        res = client.post(
            "/api/classrooms",
            json={
                "name": "向日葵班",
                "grade_id": grade_id,
                "capacity": 20,
                "head_teacher_id": teacher_id,
                "assistant_teacher_id": teacher_id,
            },
        )

        assert res.status_code == 400
        assert "同一位老師" in res.json()["detail"]

    def test_crud_flow_supports_create_update_and_soft_delete(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "classroom_admin_flow")
            grade = ClassGrade(name="中班", is_active=True)
            session.add(grade)
            teacher_a = _create_teacher(session, "T101", "陳老師")
            teacher_b = _create_teacher(session, "T102", "林老師")
            teacher_c = _create_teacher(session, "T103", "黃老師")
            session.commit()
            grade_id = grade.id
            teacher_a_id = teacher_a.id
            teacher_b_id = teacher_b.id
            teacher_c_id = teacher_c.id

        login_res = _login(client, "classroom_admin_flow")
        assert login_res.status_code == 200

        create_res = client.post(
            "/api/classrooms",
            json={
                "name": "海豚班",
                "class_code": "DOL-01",
                "grade_id": grade_id,
                "capacity": 18,
                "head_teacher_id": teacher_a_id,
            },
        )
        assert create_res.status_code == 201
        classroom_id = create_res.json()["id"]

        detail_res = client.get(f"/api/classrooms/{classroom_id}")
        assert detail_res.status_code == 200
        assert detail_res.json()["class_code"] == "DOL-01"
        assert detail_res.json()["head_teacher_id"] == teacher_a_id

        update_res = client.put(
            f"/api/classrooms/{classroom_id}",
            json={
                "name": "海豚探索班",
                "capacity": 22,
                "assistant_teacher_id": teacher_b_id,
                "art_teacher_id": teacher_c_id,
            },
        )
        assert update_res.status_code == 200

        updated_detail_res = client.get(f"/api/classrooms/{classroom_id}")
        assert updated_detail_res.status_code == 200
        updated = updated_detail_res.json()
        assert updated["name"] == "海豚探索班"
        assert updated["capacity"] == 22
        assert updated["assistant_teacher_id"] == teacher_b_id
        assert updated["art_teacher_id"] == teacher_c_id

        with session_factory() as session:
            student = Student(
                student_id="S001",
                name="小朋友甲",
                classroom_id=classroom_id,
                is_active=True,
            )
            session.add(student)
            session.commit()

        blocked_delete_res = client.delete(f"/api/classrooms/{classroom_id}")
        assert blocked_delete_res.status_code == 409
        assert "在學學生" in blocked_delete_res.json()["detail"]

        with session_factory() as session:
            student = session.query(Student).filter(Student.classroom_id == classroom_id).first()
            student.is_active = False
            session.commit()

        delete_res = client.delete(f"/api/classrooms/{classroom_id}")
        assert delete_res.status_code == 200

        list_res = client.get("/api/classrooms", params={"include_inactive": True})
        assert list_res.status_code == 200
        deleted = next(item for item in list_res.json() if item["id"] == classroom_id)
        assert deleted["is_active"] is False
