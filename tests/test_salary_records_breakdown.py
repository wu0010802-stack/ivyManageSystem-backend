"""GET /api/salaries/records 回傳每筆都帶 breakdown 鍵的回歸測試。

對應計畫 Task 2：把 services/salary/breakdown_enrollment.compute_enrollment_breakdown
整合進列表回應，前端可不需額外 round-trip 即可展開學生人數明細。
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
    SalaryRecord,
    Student,
    User,
)
from utils.auth import hash_password


@pytest.fixture
def records_client(tmp_path):
    """專屬隔離 sqlite 測試 app（含 auth + salary router + admin 帳號）。"""
    db_path = tmp_path / "salary-records-breakdown.sqlite"
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

    fake_salary_engine = MagicMock()
    fake_insurance_service = MagicMock()
    salary_module.init_salary_services(fake_salary_engine, fake_insurance_service)
    # 清掉跨測試殘留的 lazy snapshot 觸發守衛
    salary_module._snapshot_lazy_guard.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(salary_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_admin_and_login(session_factory, client):
    with session_factory() as session:
        session.add(
            User(
                employee_id=None,
                username="rec_admin",
                password_hash=hash_password("RecPass123"),
                role="admin",
                permissions=-1,
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()
    res = client.post(
        "/api/auth/login",
        json={"username": "rec_admin", "password": "RecPass123"},
    )
    assert res.status_code == 200, res.text


def _seed_teacher_with_record(session_factory, *, year: int, month: int):
    """建立大班 X 與 head_teacher_id 對應的薪資紀錄，回傳 teacher.id。"""
    with session_factory() as session:
        grade = ClassGrade(name="大班")
        session.add(grade)
        session.flush()

        teacher = Employee(
            employee_id="T100",
            name="班導測",
            title="幼兒園教師",
            position="幼兒園教師",
            employee_type="regular",
            base_salary=30000,
            hire_date=date(2024, 1, 1),
            is_active=True,
        )
        session.add(teacher)
        session.flush()

        classroom = Classroom(
            name="大班 X",
            school_year=year,
            semester=1,
            grade_id=grade.id,
            head_teacher_id=teacher.id,
            is_active=True,
        )
        session.add(classroom)
        session.flush()

        for i in range(15):
            session.add(
                Student(
                    student_id=f"X{i:03d}",
                    name=f"學生{i}",
                    classroom_id=classroom.id,
                    is_active=True,
                    enrollment_date=date(2025, 8, 1),
                    lifecycle_status="active",
                )
            )

        session.add(
            SalaryRecord(
                employee_id=teacher.id,
                salary_year=year,
                salary_month=month,
                base_salary=30000,
                festival_bonus=0,
                overtime_bonus=0,
                overtime_pay=0,
                gross_salary=30000,
                total_deduction=0,
                net_salary=30000,
                version=1,
                is_finalized=False,
            )
        )
        session.commit()
        return teacher.id


def test_records_list_includes_breakdown_for_teacher(records_client):
    """GET /api/salaries/records 每筆都帶 breakdown 鍵；班導者填入 enrollment 明細。"""
    client, session_factory = records_client
    _seed_admin_and_login(session_factory, client)
    _seed_teacher_with_record(session_factory, year=2026, month=5)

    response = client.get("/api/salaries/records?year=2026&month=5")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    row = rows[0]

    assert "breakdown" in row
    assert row["breakdown"] is not None
    enrollment = row["breakdown"]["enrollment"]
    assert enrollment["snapshot_date"] == "2026-05-31"
    assert enrollment["total"] == 15
    assert enrollment["classroom_name"] == "大班 X"
    assert enrollment["grade_name"] == "大班"
    assert row["breakdown"]["assistant"] is None
