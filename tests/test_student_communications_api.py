"""家長溝通紀錄 CRUD 端點整合測試。"""

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
from models.database import Base, Classroom, Student, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "student-comm.sqlite"
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
    app.include_router(comm_router)

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
    perms=Permission.STUDENTS_READ | Permission.STUDENTS_WRITE,
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
    _add_user(session)
    classroom = Classroom(name="大象班", is_active=True)
    session.add(classroom)
    session.flush()
    target = Student(
        student_id="S001", name="小明", classroom_id=classroom.id, is_active=True
    )
    other = Student(
        student_id="S002", name="小華", classroom_id=classroom.id, is_active=True
    )
    session.add_all([target, other])
    session.commit()
    return {"target": target.id, "other": other.id}


class TestCommunicationsCRUD:
    def test_create_and_list_by_student(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
        _login(client)

        payload = {
            "student_id": ids["target"],
            "communication_date": "2026-04-10",
            "communication_type": "電話",
            "topic": "遲到提醒",
            "content": "告知家長近期上午到校時間較晚",
            "follow_up": "下週觀察",
        }
        res = client.post("/api/students/communications", json=payload)
        assert res.status_code == 201
        data = res.json()
        assert data["student_id"] == ids["target"]
        assert data["student_name"] == "小明"
        assert data["topic"] == "遲到提醒"

        # 另一個學生一筆，確認不會被誤撈
        client.post(
            "/api/students/communications",
            json={
                "student_id": ids["other"],
                "communication_date": "2026-04-11",
                "communication_type": "LINE",
                "content": "其他學生的溝通",
            },
        )

        list_res = client.get(
            "/api/students/communications", params={"student_id": ids["target"]}
        )
        assert list_res.status_code == 200
        body = list_res.json()
        assert body["total"] == 1
        assert body["items"][0]["topic"] == "遲到提醒"

    def test_update_and_delete(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
        _login(client)

        created = client.post(
            "/api/students/communications",
            json={
                "student_id": ids["target"],
                "communication_date": "2026-04-10",
                "communication_type": "面談",
                "content": "初步觀察",
            },
        ).json()
        log_id = created["id"]

        up = client.put(
            f"/api/students/communications/{log_id}",
            json={"topic": "家訪紀錄", "follow_up": "後續追蹤"},
        )
        assert up.status_code == 200
        assert up.json()["topic"] == "家訪紀錄"
        assert up.json()["follow_up"] == "後續追蹤"

        de = client.delete(f"/api/students/communications/{log_id}")
        assert de.status_code == 200

        list_res = client.get(
            "/api/students/communications", params={"student_id": ids["target"]}
        )
        assert list_res.json()["total"] == 0

    def test_validation_invalid_type(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
        _login(client)

        res = client.post(
            "/api/students/communications",
            json={
                "student_id": ids["target"],
                "communication_date": "2026-04-10",
                "communication_type": "碰面",  # 非合法選項
                "content": "x",
            },
        )
        assert res.status_code == 422

    def test_validation_empty_content(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
        _login(client)

        res = client.post(
            "/api/students/communications",
            json={
                "student_id": ids["target"],
                "communication_date": "2026-04-10",
                "communication_type": "電話",
                "content": "   ",  # 全空白
            },
        )
        assert res.status_code == 422

    def test_404_on_unknown_student(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            _seed(s)
        _login(client)

        res = client.post(
            "/api/students/communications",
            json={
                "student_id": 99999,
                "communication_date": "2026-04-10",
                "communication_type": "電話",
                "content": "x",
            },
        )
        assert res.status_code == 404

    def test_date_range_filter(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
        _login(client)

        for d in ("2026-02-01", "2026-03-15", "2026-04-20"):
            client.post(
                "/api/students/communications",
                json={
                    "student_id": ids["target"],
                    "communication_date": d,
                    "communication_type": "電話",
                    "content": f"comm {d}",
                },
            )

        res = client.get(
            "/api/students/communications",
            params={
                "student_id": ids["target"],
                "date_from": "2026-03-01",
                "date_to": "2026-04-01",
            },
        )
        assert res.status_code == 200
        assert res.json()["total"] == 1
        assert res.json()["items"][0]["communication_date"] == "2026-03-15"

    def test_requires_students_read(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            _add_user(s, username="forbidden", perms=Permission.CLASSROOMS_READ)
            s.commit()
        _login(client, username="forbidden")
        res = client.get("/api/students/communications")
        assert res.status_code == 403

    def test_requires_students_write_for_create(self, client_with_db):
        client, factory = client_with_db
        with factory() as s:
            ids = _seed(s)
            _add_user(s, username="readonly", perms=Permission.STUDENTS_READ)
            s.commit()
        _login(client, username="readonly")
        res = client.post(
            "/api/students/communications",
            json={
                "student_id": ids["target"],
                "communication_date": "2026-04-10",
                "communication_type": "電話",
                "content": "x",
            },
        )
        assert res.status_code == 403
