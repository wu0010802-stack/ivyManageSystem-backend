"""Portal my students 顯示文案回歸測試。"""

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
from api.portal.students import router as portal_students_router
from models.database import Base, Classroom, Employee, Student, User
from utils.auth import hash_password


@pytest.fixture
def portal_students_client(tmp_path):
    db_path = tmp_path / "portal-students.sqlite"
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
    app.include_router(portal_students_router, prefix="/api/portal")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_employee(session, employee_id: str, name: str) -> Employee:
    employee = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=32000,
        is_active=True,
    )
    session.add(employee)
    session.flush()
    return employee


def _create_user(session, username: str, password: str, employee: Employee) -> User:
    user = User(
        employee_id=employee.id,
        username=username,
        password_hash=hash_password(password),
        role="teacher",
        permissions=0,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


class TestPortalMyStudents:
    def test_english_teacher_role_label_is_exposed_to_users(self, portal_students_client):
        client, session_factory = portal_students_client

        with session_factory() as session:
            english_teacher = _create_employee(session, "T500", "Amy 老師")
            _create_user(session, "english_teacher", "TempPass123", english_teacher)
            classroom = Classroom(
                name="彩虹班",
                school_year=2025,
                semester=2,
                art_teacher_id=english_teacher.id,
                is_active=True,
            )
            session.add(classroom)
            session.flush()
            session.add(Student(student_id="S888", name="小花", classroom_id=classroom.id, is_active=True))
            session.commit()

        login_res = _login(client, "english_teacher", "TempPass123")
        assert login_res.status_code == 200

        res = client.get("/api/portal/my-students")

        assert res.status_code == 200
        assert res.json()["classrooms"][0]["role"] == "美語老師"

    def test_students_grouped_correctly_for_multiple_classrooms(self, portal_students_client):
        """教師跨多班時，學生應正確依班級分組，不重複"""
        client, session_factory = portal_students_client

        with session_factory() as session:
            teacher = _create_employee(session, "T600", "跨班老師")
            _create_user(session, "multi_class_teacher", "TempPass123", teacher)

            cr1 = Classroom(name="A班", school_year=2025, semester=2, head_teacher_id=teacher.id, is_active=True)
            cr2 = Classroom(name="B班", school_year=2025, semester=2, assistant_teacher_id=teacher.id, is_active=True)
            cr3 = Classroom(name="C班", school_year=2025, semester=2, art_teacher_id=teacher.id, is_active=True)
            session.add_all([cr1, cr2, cr3])
            session.flush()

            # A班 2 個學生，B班 1 個，C班 0 個
            session.add_all([
                Student(student_id="A01", name="學生甲", classroom_id=cr1.id, is_active=True),
                Student(student_id="A02", name="學生乙", classroom_id=cr1.id, is_active=True),
                Student(student_id="B01", name="學生丙", classroom_id=cr2.id, is_active=True),
            ])
            session.commit()

        login_res = _login(client, "multi_class_teacher", "TempPass123")
        assert login_res.status_code == 200

        res = client.get("/api/portal/my-students")
        assert res.status_code == 200

        data = res.json()
        assert data["total_students"] == 3

        classrooms_by_name = {c["classroom_name"]: c for c in data["classrooms"]}
        assert classrooms_by_name["A班"]["student_count"] == 2
        assert classrooms_by_name["B班"]["student_count"] == 1
        assert classrooms_by_name["C班"]["student_count"] == 0

        # 確認 A班學生按姓名排序
        a_names = [s["name"] for s in classrooms_by_name["A班"]["students"]]
        assert a_names == sorted(a_names)
