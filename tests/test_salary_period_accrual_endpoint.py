"""
整合測試：GET /api/salaries/festival-bonus/period-accrual
"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import api.salary as salary_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.salary import router as salary_router
from models.database import (
    Base,
    ClassGrade,
    Classroom,
    Employee,
    Student,
    User,
)
from services.salary_engine import SalaryEngine
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "period-accrual-endpoint.sqlite"
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

    real_engine = SalaryEngine(load_from_db=False)
    salary_module.init_salary_services(real_engine, MagicMock())

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(salary_router)

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_admin(session):
    user = User(
        username="admin",
        password_hash=hash_password("pw"),
        role="admin",
        permissions=int(Permission.SALARY_READ),
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.commit()


def _login(client, username="admin", password="pw"):
    """登入後 access_token 由 set-cookie 自動存入 TestClient.cookies。"""
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text
    return None


def _seed_teacher(session):
    grade = ClassGrade(name="大班")
    session.add(grade)
    session.flush()

    emp = Employee(
        employee_id="E_T001",
        name="王老師",
        title="幼兒園教師",
        position="幼兒園教師",
        base_salary=35000,
        hire_date=date(2024, 1, 1),
        is_active=True,
    )
    session.add(emp)
    session.flush()
    classroom = Classroom(
        name="向日葵班",
        grade_id=grade.id,
        head_teacher_id=emp.id,
        assistant_teacher_id=0,
        is_active=True,
    )
    session.add(classroom)
    session.flush()
    emp.classroom_id = classroom.id
    for i in range(20):
        session.add(
            Student(
                student_id=f"ST{i:03d}",
                name=f"S{i}",
                classroom_id=classroom.id,
                enrollment_date=date(2024, 1, 1),
                is_active=True,
            )
        )
    session.commit()
    return emp


class TestPeriodAccrualEndpoint:
    def test_distribution_month_returns_empty(self, client):
        c, sf = client
        with sf() as s:
            _create_admin(s)
        _login(c)

        r = c.get(
            "/api/salaries/festival-bonus/period-accrual?year=2026&month=6",
        )
        assert r.status_code == 200
        data = r.json()
        assert data["is_distribution_month"] is True
        assert data["rows"] == []

    def test_april_returns_three_months(self, client):
        c, sf = client
        with sf() as s:
            _create_admin(s)
            _seed_teacher(s)
        _login(c)

        r = c.get(
            "/api/salaries/festival-bonus/period-accrual?year=2026&month=4",
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["is_distribution_month"] is False
        assert data["period_start_month"] == 2
        assert data["current_month"] == 4
        assert data["distribution_month"] == 6
        assert len(data["rows"]) == 1
        row = data["rows"][0]
        assert row["name"] == "王老師"
        assert len(row["monthly"]) == 3
        assert [(m["year"], m["month"]) for m in row["monthly"]] == [
            (2026, 2),
            (2026, 3),
            (2026, 4),
        ]
        totals = row["totals"]
        assert totals["festival_bonus"] == sum(
            m["festival_bonus"] for m in row["monthly"]
        )
        assert totals["overtime_bonus"] == sum(
            m["overtime_bonus"] for m in row["monthly"]
        )
        assert totals["meeting_absence_deduction"] == sum(
            m["meeting_absence_deduction"] for m in row["monthly"]
        )
        assert totals["net_estimate"] == max(
            0,
            totals["festival_bonus"]
            + totals["overtime_bonus"]
            - totals["meeting_absence_deduction"],
        )

    def test_unauthenticated_returns_401(self, client):
        c, _ = client
        r = c.get("/api/salaries/festival-bonus/period-accrual?year=2026&month=4")
        assert r.status_code in (401, 403)

    def test_january_crosses_previous_year(self, client):
        c, sf = client
        with sf() as s:
            _create_admin(s)
            _seed_teacher(s)
        _login(c)

        r = c.get(
            "/api/salaries/festival-bonus/period-accrual?year=2026&month=1",
        )
        assert r.status_code == 200
        data = r.json()
        assert data["period_start_month"] == 12
        row = data["rows"][0]
        assert [(m["year"], m["month"]) for m in row["monthly"]] == [
            (2025, 12),
            (2026, 1),
        ]
