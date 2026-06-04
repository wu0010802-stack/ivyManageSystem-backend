"""tests/test_provenance_api.py — GET /api/provenance/{key} 端點測試。

使用 FastAPI TestClient + SQLite in-memory DB，
透過 /api/auth/login 取得 cookie 後打 provenance 端點。
模式完全仿照 test_year_end_grid_api.py。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module  # noqa: E402
from api.auth import _account_failures, _ip_attempts  # noqa: E402
from api.auth import router as auth_router  # noqa: E402
from api.provenance import provenance_router  # noqa: E402
from models.attendance import Attendance  # noqa: E402
from models.config import BonusConfig  # noqa: E402
from models.database import Base, User  # noqa: E402
from models.employee import Employee  # noqa: E402
from models.year_end import YearEndCycle  # noqa: E402
from utils.auth import hash_password  # noqa: E402

# ============================================================
# Fixtures
# ============================================================

READ_PERMS = ["YEAR_END_READ"]

ACADEMIC_YEAR = 114
CYCLE_START = date(2025, 8, 1)
CYCLE_END = date(2026, 7, 31)
BONUS_CALC_DATE = date(2026, 1, 15)


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "provenance-api-test.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(provenance_router)

    with TestClient(app) as client:
        yield client, sf

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


# ============================================================
# Seed helpers
# ============================================================


def _seed_users(sf):
    with sf() as s:
        s.add(
            User(
                username="reader",
                password_hash=hash_password("TempPass123"),
                role="staff",
                permission_names=READ_PERMS,
                is_active=True,
            )
        )
        s.commit()


def _seed_cycle_and_employee(sf) -> tuple[int, int]:
    """種最小必要資料：cycle + 一位月薪員工 + active BonusConfig。
    回傳 (cycle_id, employee_id)。
    """
    with sf() as s:
        s.add(
            BonusConfig(
                config_year=2025,
                version=1,
                is_active=True,
                head_teacher_ab=2000,
                head_teacher_c=1500,
                assistant_teacher_ab=1200,
                assistant_teacher_c=1200,
                principal_festival=6500,
                director_festival=3500,
                leader_festival=2000,
                driver_festival=1000,
                designer_festival=1000,
                admin_festival=2000,
                art_teacher_festival=2000,
                late_deduction_per_time=50,
                missing_punch_deduction_per_time=50,
            )
        )
        s.flush()

        cycle = YearEndCycle(
            academic_year=ACADEMIC_YEAR,
            start_date=CYCLE_START,
            end_date=CYCLE_END,
            bonus_calc_date=BONUS_CALC_DATE,
        )
        s.add(cycle)
        s.flush()

        emp = Employee(
            employee_id="E_PROV_001",
            name="王老師",
            position="班導",
            title="幼兒園教師",
            base_salary=36160,
            is_active=True,
            hire_date=date(2020, 1, 1),
        )
        s.add(emp)
        s.flush()

        s.commit()
        cycle_id = cycle.id
        emp_id = emp.id

    return cycle_id, emp_id


def _seed_late_attendance(sf, emp_id: int, on_date: date):
    """種一筆 is_late=True 的考勤紀錄。"""
    with sf() as s:
        s.add(
            Attendance(
                employee_id=emp_id,
                attendance_date=on_date,
                is_late=True,
                is_missing_punch_in=False,
                is_missing_punch_out=False,
            )
        )
        s.commit()


def _login(client, username="reader"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": "TempPass123"}
    )
    assert res.status_code == 200, f"login failed: {res.text}"


# ============================================================
# Tests
# ============================================================


def test_provenance_attendance_late_200(client_with_db):
    """200 OK：seed 1 筆遲到 → attendance_late, value==-50, 1 source_record。"""
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, emp_id = _seed_cycle_and_employee(sf)
    # 民國曆年 114 → 2025-01-01..2025-12-31
    _seed_late_attendance(sf, emp_id, date(2025, 3, 1))
    _login(client)

    res = client.get(
        f"/api/provenance/attendance_late",
        params={"cycle_id": cycle_id, "employee_id": emp_id},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["key"] == "attendance_late"
    assert Decimal(str(body["value"])) == Decimal("-50")
    assert len(body["source_records"]) == 1
    assert body["source_records"][0]["module"] == "attendance"


def test_provenance_unknown_key_400(client_with_db):
    """400：未知 key 'bogus' → 400 錯誤。"""
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, emp_id = _seed_cycle_and_employee(sf)
    _login(client)

    res = client.get(
        "/api/provenance/bogus",
        params={"cycle_id": cycle_id, "employee_id": emp_id},
    )
    assert res.status_code == 400, res.text


def test_provenance_missing_cycle_404(client_with_db):
    """404：cycle_id=999999 不存在 → 404。"""
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, emp_id = _seed_cycle_and_employee(sf)
    _login(client)

    res = client.get(
        "/api/provenance/attendance_late",
        params={"cycle_id": 999999, "employee_id": emp_id},
    )
    assert res.status_code == 404, res.text


def test_provenance_missing_employee_404(client_with_db):
    """404：employee_id=999999 不存在 → 404。"""
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, emp_id = _seed_cycle_and_employee(sf)
    _login(client)

    res = client.get(
        "/api/provenance/attendance_late",
        params={"cycle_id": cycle_id, "employee_id": 999999},
    )
    assert res.status_code == 404, res.text
