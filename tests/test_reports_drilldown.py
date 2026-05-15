"""Reports drill-down endpoints 測試（P2）。"""

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
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.reports import router as reports_router
from models.database import (
    Attendance,
    Base,
    Classroom,
    Employee,
    SalaryRecord,
    User,
)
from utils.auth import hash_password


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "drilldown.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(reports_router)
    with TestClient(app) as c:
        yield c, session_factory
    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_e
    base_module._SessionFactory = old_sf
    engine.dispose()


def _login(client, sf, username="admin", role="admin", permissions=-1):
    with sf() as s:
        s.add(
            User(
                username=username,
                password_hash=hash_password("Passw0rd!"),
                role=role,
                permissions=permissions,
                is_active=True,
                must_change_password=False,
            )
        )
        s.commit()
    r = client.post(
        "/api/auth/login", json={"username": username, "password": "Passw0rd!"}
    )
    assert r.status_code == 200, r.text


def _seed_attendance_anomalies(sf):
    """造 5 筆異常考勤 (2026 年 3-4 月) + 1 筆正常考勤。"""
    with sf() as s:
        s.add_all(
            [
                Classroom(id=10, name="A 班", is_active=True),
                Classroom(id=20, name="B 班", is_active=True),
                Employee(
                    id=1,
                    employee_id="E1",
                    name="王老師",
                    classroom_id=10,
                    position="老師",
                    employee_type="regular",
                    is_active=True,
                ),
                Employee(
                    id=2,
                    employee_id="E2",
                    name="陳老師",
                    classroom_id=20,
                    position="老師",
                    employee_type="regular",
                    is_active=True,
                ),
            ]
        )
        s.commit()
        s.add_all(
            [
                Attendance(
                    employee_id=1,
                    attendance_date=date(2026, 3, 5),
                    is_late=True,
                    late_minutes=10,
                ),
                Attendance(
                    employee_id=1,
                    attendance_date=date(2026, 3, 12),
                    is_early_leave=True,
                    early_leave_minutes=8,
                ),
                Attendance(
                    employee_id=1,
                    attendance_date=date(2026, 3, 20),
                    is_missing_punch_in=True,
                ),
                Attendance(
                    employee_id=2,
                    attendance_date=date(2026, 3, 7),
                    is_late=True,
                    late_minutes=5,
                ),
                Attendance(
                    employee_id=2,
                    attendance_date=date(2026, 4, 3),
                    is_late=True,
                    late_minutes=15,
                ),
                # 正常考勤（不應出現在 anomalies）
                Attendance(
                    employee_id=1,
                    attendance_date=date(2026, 3, 25),
                ),
            ]
        )
        s.commit()


def test_attendance_detail_no_filters_returns_anomalies(client):
    c, sf = client
    _login(c, sf)
    _seed_attendance_anomalies(sf)
    r = c.get("/api/reports/attendance/detail?year=2026")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["year"] == 2026
    assert data["month"] is None
    assert data["classroom_id"] is None
    assert data["total_records"] == 5  # 5 筆異常（不含 normal）
    assert data["truncated"] is False
    types = [set(rec["anomaly_types"]) for rec in data["records"]]
    assert {"late"} in types
    assert {"early_leave"} in types


def test_attendance_detail_filtered_by_month(client):
    c, sf = client
    _login(c, sf)
    _seed_attendance_anomalies(sf)
    r = c.get("/api/reports/attendance/detail?year=2026&month=3")
    assert r.status_code == 200
    data = r.json()
    assert data["month"] == 3
    assert data["total_records"] == 4  # 3 月只 4 筆（不含 4/3 的）
    for rec in data["records"]:
        assert rec["date"].startswith("2026-03-")


def test_attendance_detail_filtered_by_classroom(client):
    c, sf = client
    _login(c, sf)
    _seed_attendance_anomalies(sf)
    r = c.get("/api/reports/attendance/detail?year=2026&classroom_id=10")
    assert r.status_code == 200
    data = r.json()
    assert data["classroom_id"] == 10
    assert data["total_records"] == 3  # employee_id=1 全部異常筆數
    for rec in data["records"]:
        assert rec["classroom_id"] == 10


def test_attendance_detail_no_permission_returns_403(client):
    c, sf = client
    _login(c, sf, username="reader", role="staff", permissions=0)
    r = c.get("/api/reports/attendance/detail?year=2026")
    assert r.status_code == 403


def test_attendance_detail_truncates_at_200(client, monkeypatch):
    c, sf = client
    _login(c, sf)
    from api import reports as reports_mod

    monkeypatch.setattr(reports_mod, "ATTENDANCE_DETAIL_LIMIT", 3)
    _seed_attendance_anomalies(sf)  # 5 筆異常
    r = c.get("/api/reports/attendance/detail?year=2026")
    data = r.json()
    assert data["total_records"] == 5
    assert len(data["records"]) == 3
    assert data["truncated"] is True


def _seed_salary_contributors(sf):
    """造 6 筆 2026/3 已封存薪資 + 1 筆草稿（不入榜）。"""
    with sf() as s:
        s.add_all(
            [
                Employee(id=11, employee_id="C11", name="王老師", position="老師"),
                Employee(id=12, employee_id="C12", name="陳老師", position="老師"),
                Employee(id=13, employee_id="C13", name="李老師", position="老師"),
                Employee(id=14, employee_id="C14", name="林老師", position="老師"),
                Employee(id=15, employee_id="C15", name="張老師", position="老師"),
                Employee(id=16, employee_id="C16", name="黃老師", position="老師"),
                Employee(
                    id=17, employee_id="C17", name="周老師（草稿）", position="老師"
                ),
            ]
        )
        s.commit()
        s.add_all(
            [
                SalaryRecord(
                    employee_id=11,
                    salary_year=2026,
                    salary_month=3,
                    gross_salary=70000,
                    net_salary=60000,
                    overtime_pay=5000,
                    is_finalized=True,
                    needs_recalc=False,
                ),
                SalaryRecord(
                    employee_id=12,
                    salary_year=2026,
                    salary_month=3,
                    gross_salary=65000,
                    net_salary=55000,
                    overtime_pay=12000,
                    is_finalized=True,
                    needs_recalc=False,
                ),
                SalaryRecord(
                    employee_id=13,
                    salary_year=2026,
                    salary_month=3,
                    gross_salary=60000,
                    net_salary=50000,
                    overtime_pay=8000,
                    is_finalized=True,
                    needs_recalc=False,
                ),
                SalaryRecord(
                    employee_id=14,
                    salary_year=2026,
                    salary_month=3,
                    gross_salary=55000,
                    net_salary=45000,
                    overtime_pay=3000,
                    is_finalized=True,
                    needs_recalc=False,
                ),
                SalaryRecord(
                    employee_id=15,
                    salary_year=2026,
                    salary_month=3,
                    gross_salary=50000,
                    net_salary=42000,
                    overtime_pay=0,
                    is_finalized=True,
                    needs_recalc=False,
                ),
                SalaryRecord(
                    employee_id=16,
                    salary_year=2026,
                    salary_month=3,
                    gross_salary=45000,
                    net_salary=38000,
                    overtime_pay=2000,
                    is_finalized=True,
                    needs_recalc=False,
                ),
                SalaryRecord(
                    employee_id=17,
                    salary_year=2026,
                    salary_month=3,
                    gross_salary=999999,
                    net_salary=999999,
                    overtime_pay=999999,
                    is_finalized=False,
                    needs_recalc=True,
                ),
            ]
        )
        s.commit()


def test_salary_contributors_top5_gross_and_overtime(client):
    c, sf = client
    _login(c, sf)
    _seed_salary_contributors(sf)
    r = c.get("/api/reports/salary/contributors?year=2026&month=3")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["year"] == 2026
    assert data["month"] == 3
    assert len(data["top_gross"]) == 5
    gross_amounts = [rec["gross_salary"] for rec in data["top_gross"]]
    assert gross_amounts == [70000, 65000, 60000, 55000, 50000]
    assert len(data["top_overtime"]) == 5
    ot_amounts = [rec["overtime_pay"] for rec in data["top_overtime"]]
    assert ot_amounts == [12000, 8000, 5000, 3000, 2000]
    all_ids = {r["employee_id"] for r in data["top_gross"] + data["top_overtime"]}
    assert 17 not in all_ids


def test_salary_contributors_masks_amount_for_non_admin(client):
    c, sf = client
    _login(c, sf, username="viewer", role="staff", permissions=1 << 13)
    _seed_salary_contributors(sf)
    r = c.get("/api/reports/salary/contributors?year=2026&month=3")
    assert r.status_code == 200, r.text
    data = r.json()
    for rec in data["top_gross"]:
        assert rec["gross_salary"] is None
        assert rec["employee_name"]
    for rec in data["top_overtime"]:
        assert rec["overtime_pay"] is None


def test_salary_contributors_excludes_draft_salaries(client):
    c, sf = client
    _login(c, sf)
    _seed_salary_contributors(sf)
    r = c.get("/api/reports/salary/contributors?year=2026&month=3")
    data = r.json()
    all_employee_ids = {
        rec["employee_id"] for rec in data["top_gross"] + data["top_overtime"]
    }
    assert 17 not in all_employee_ids
