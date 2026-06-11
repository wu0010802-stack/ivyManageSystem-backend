"""Assessment 關聯事件 FK 測試。

涵蓋：合法 incident_id 寫入、他生 incident_id 阻擋（422）、
incident 刪除後 ON DELETE SET NULL。
"""

import os
import sys
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.student_assessments import router as student_assessments_router
from models.database import (
    Base,
    Classroom,
    Student,
    StudentAssessment,
    StudentIncident,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "assessment-incident.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    # SQLite 預設不啟用 FK，ON DELETE SET NULL 才會生效
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(student_assessments_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, username, perms, password="TempPass123") -> User:
    if isinstance(perms, str):
        perms = [perms]
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permission_names=perms,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password="TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _seed_classroom_and_students(session):
    cls = Classroom(name="星星班", is_active=True)
    session.add(cls)
    session.flush()
    s1 = Student(student_id="A001", name="阿明", classroom_id=cls.id, is_active=True)
    s2 = Student(student_id="A002", name="小華", classroom_id=cls.id, is_active=True)
    session.add_all([s1, s2])
    session.flush()
    return cls, s1, s2


class TestAssessmentRelatedIncident:
    def test_create_with_valid_incident_id_succeeds(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(
                session,
                "admin1",
                ["STUDENTS_READ", "STUDENTS_WRITE"],
            )
            _, s1, _ = _seed_classroom_and_students(session)
            inc = StudentIncident(
                student_id=s1.id,
                incident_type="行為觀察",
                occurred_at=datetime(2026, 3, 10, 10, 0),
                description="搶玩具",
            )
            session.add(inc)
            session.flush()
            inc_id = inc.id
            s1_id = s1.id
            session.commit()

        assert _login(client, "admin1").status_code == 200
        res = client.post(
            "/api/student-assessments",
            json={
                "student_id": s1_id,
                "semester": "2025下",
                "assessment_type": "期中",
                "content": "情緒管理需加強",
                "assessment_date": "2026-03-15",
                "related_incident_id": inc_id,
            },
        )
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["related_incident_id"] == inc_id
        assert body["related_incident"]["incident_type"] == "行為觀察"

    def test_create_with_other_students_incident_returns_422(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(
                session,
                "admin1",
                ["STUDENTS_READ", "STUDENTS_WRITE"],
            )
            _, s1, s2 = _seed_classroom_and_students(session)
            other_inc = StudentIncident(
                student_id=s2.id,
                incident_type="意外受傷",
                occurred_at=datetime(2026, 3, 10, 10, 0),
                description="跌倒",
            )
            session.add(other_inc)
            session.flush()
            other_id = other_inc.id
            s1_id = s1.id
            session.commit()

        assert _login(client, "admin1").status_code == 200
        res = client.post(
            "/api/student-assessments",
            json={
                "student_id": s1_id,
                "semester": "2025下",
                "assessment_type": "期中",
                "content": "x",
                "assessment_date": "2026-03-15",
                "related_incident_id": other_id,
            },
        )
        assert res.status_code == 422
        assert "不屬於同一位學生" in res.json()["detail"]

    def test_create_with_unknown_incident_returns_422(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(
                session,
                "admin1",
                ["STUDENTS_READ", "STUDENTS_WRITE"],
            )
            _, s1, _ = _seed_classroom_and_students(session)
            s1_id = s1.id
            session.commit()

        assert _login(client, "admin1").status_code == 200
        res = client.post(
            "/api/student-assessments",
            json={
                "student_id": s1_id,
                "semester": "2025下",
                "assessment_type": "期中",
                "content": "x",
                "assessment_date": "2026-03-15",
                "related_incident_id": 9999,
            },
        )
        assert res.status_code == 422

    def test_incident_delete_sets_assessment_fk_to_null(self, client_with_db):
        """ON DELETE SET NULL：刪除 incident 後評量保留、related_incident_id 變 NULL。"""
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(
                session,
                "admin1",
                ["STUDENTS_READ", "STUDENTS_WRITE"],
            )
            _, s1, _ = _seed_classroom_and_students(session)
            inc = StudentIncident(
                student_id=s1.id,
                incident_type="行為觀察",
                occurred_at=datetime(2026, 3, 10, 10, 0),
                description="x",
            )
            session.add(inc)
            session.flush()
            asm = StudentAssessment(
                student_id=s1.id,
                semester="2025下",
                assessment_type="期中",
                content="y",
                assessment_date=date(2026, 3, 15),
                related_incident_id=inc.id,
            )
            session.add(asm)
            session.flush()
            asm_id = asm.id
            inc_id = inc.id
            session.commit()

        with session_factory() as session:
            inc = session.query(StudentIncident).filter_by(id=inc_id).one()
            session.delete(inc)
            session.commit()

        with session_factory() as session:
            asm_after = session.query(StudentAssessment).filter_by(id=asm_id).one()
            assert asm_after.related_incident_id is None
            assert asm_after.content == "y"  # 評量內容保留


# ── R5-1：評量 list 須排除終態學生（手寫 classroom 檢查漏 lifecycle）──


def test_list_assessments_excludes_terminal_student(client_with_db):
    """持 STUDENTS_READ 的非管理角色，自班學生轉終態（畢業）後不可再讀其評量
    （_require_classroom_access 漏 lifecycle 過濾，且終態學生保留 classroom_id）。"""
    from models.employee import Employee
    from models.classroom import LIFECYCLE_GRADUATED

    client, sf = client_with_db
    with sf() as s:
        emp = Employee(
            employee_id="T1", name="老師甲", is_active=True, base_salary=30000
        )
        s.add(emp)
        s.flush()
        cls = Classroom(name="星星班", is_active=True, head_teacher_id=emp.id)
        s.add(cls)
        s.flush()
        stu = Student(
            student_id="G001",
            name="畢業生",
            classroom_id=cls.id,
            is_active=False,
            lifecycle_status=LIFECYCLE_GRADUATED,
        )
        s.add(stu)
        s.flush()
        s.add(
            StudentAssessment(
                student_id=stu.id,
                semester="2025下",
                assessment_type="期中",
                content="機密評語",
                assessment_date=date(2026, 3, 15),
            )
        )
        s.add(
            User(
                username="t1",
                password_hash=hash_password("TempPass123"),
                role="teacher",
                employee_id=emp.id,
                permission_names=["STUDENTS_READ"],
                is_active=True,
                token_version=0,
            )
        )
        s.commit()
        stu_id = stu.id

    assert _login(client, "t1").status_code == 200
    res = client.get(f"/api/student-assessments?student_id={stu_id}")
    assert res.status_code in (200, 403), res.text
    if res.status_code == 200:
        assert res.json()["total"] == 0, "終態學生評量不應外洩給前班導"
