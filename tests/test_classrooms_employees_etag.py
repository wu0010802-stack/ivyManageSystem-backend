"""GET /api/classrooms 與 GET /api/employees 的 ETag 整合測試。

驗證：
- 回應帶 ETag 與 Cache-Control: private, no-cache
- 帶相同 If-None-Match 命中 304（無 body）
- 內容變動後 ETag 改變、原 If-None-Match 不再命中

不在此測試「per-user payload 變動造成 ETag 不同」，因 _format_employee_response
的 can_view_salary 行為已由 test_employees_amount_guard 覆蓋。
"""

from __future__ import annotations

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
from api.classrooms import router as classrooms_router
from api.employees import router as employees_router
from models.base import Base
from models.database import ClassGrade, Classroom, Employee, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "etag.sqlite"
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
    app.include_router(employees_router)

    with TestClient(app) as test_client:
        with session_factory() as session:
            user = User(
                username="etag_admin",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permissions=Permission.CLASSROOMS_READ
                | Permission.CLASSROOMS_WRITE
                | Permission.EMPLOYEES_READ
                | Permission.EMPLOYEES_WRITE,
                is_active=True,
            )
            session.add(user)
            session.commit()

        login = test_client.post(
            "/api/auth/login",
            json={"username": "etag_admin", "password": "TempPass123"},
        )
        assert login.status_code == 200
        yield test_client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_classroom(session_factory, name: str = "ETag 班") -> int:
    """新增一個班級；ClassGrade 重用既有「小班」（UNIQUE name）。"""
    with session_factory() as session:
        grade = session.query(ClassGrade).filter_by(name="小班").first()
        if grade is None:
            grade = ClassGrade(name="小班", is_active=True)
            session.add(grade)
            session.flush()
        cls = Classroom(
            name=name,
            capacity=20,
            school_year=114,
            semester=2,
            grade_id=grade.id,
            is_active=True,
        )
        session.add(cls)
        session.commit()
        return cls.id


def _seed_employee(session_factory, name: str = "ETag 員工") -> int:
    with session_factory() as session:
        emp = Employee(
            employee_id=f"E{name}",
            name=name,
            base_salary=32000,
            is_active=True,
        )
        session.add(emp)
        session.commit()
        return emp.id


class TestClassroomsEtag:
    def test_response_has_etag_and_private_cache_control(self, client):
        test_client, session_factory = client
        _seed_classroom(session_factory)

        res = test_client.get("/api/classrooms")

        assert res.status_code == 200
        assert res.headers.get("ETag")
        assert res.headers["ETag"].startswith('"')
        cache_control = res.headers.get("Cache-Control", "")
        assert (
            "private" in cache_control
        ), f"Cache-Control 必須含 private，實際: {cache_control!r}"
        assert "no-cache" in cache_control

    def test_if_none_match_hit_returns_304_without_body(self, client):
        test_client, session_factory = client
        _seed_classroom(session_factory)

        first = test_client.get("/api/classrooms")
        etag = first.headers["ETag"]

        second = test_client.get("/api/classrooms", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.content == b""
        assert second.headers["ETag"] == etag

    def test_payload_change_invalidates_etag(self, client):
        test_client, session_factory = client
        _seed_classroom(session_factory)

        first = test_client.get("/api/classrooms")
        old_etag = first.headers["ETag"]

        # 新增一個班級 → payload 變動
        _seed_classroom(session_factory, name="ETag 班 2")

        third = test_client.get("/api/classrooms", headers={"If-None-Match": old_etag})
        assert third.status_code == 200
        assert third.headers["ETag"] != old_etag


class TestEmployeesEtag:
    def test_response_has_etag_and_private_cache_control(self, client):
        test_client, session_factory = client
        _seed_employee(session_factory)

        res = test_client.get("/api/employees")

        assert res.status_code == 200
        assert res.headers.get("ETag")
        cache_control = res.headers.get("Cache-Control", "")
        assert "private" in cache_control
        assert "no-cache" in cache_control

    def test_if_none_match_hit_returns_304(self, client):
        test_client, session_factory = client
        _seed_employee(session_factory)

        first = test_client.get("/api/employees")
        etag = first.headers["ETag"]

        second = test_client.get("/api/employees", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.content == b""

    def test_payload_change_invalidates_etag(self, client):
        test_client, session_factory = client
        _seed_employee(session_factory, "員工 A")

        first = test_client.get("/api/employees")
        old_etag = first.headers["ETag"]

        _seed_employee(session_factory, "員工 B")

        second = test_client.get("/api/employees", headers={"If-None-Match": old_etag})
        assert second.status_code == 200
        assert second.headers["ETag"] != old_etag
