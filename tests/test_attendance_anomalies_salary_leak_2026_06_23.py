"""資安回歸：考勤異常端點透過 estimated_deduction 洩漏底薪（跨權限邊界）。

漏洞：GET /api/attendance/anomalies 與 /anomalies/export 僅以 ATTENDANCE_READ
守衛，但每筆「遲到」row 回傳 estimated_deduction = round_half_up(
calc_daily_salary(base_salary)/8/60 × late_minutes)。因 calc_daily_salary(base)
= base/30，可逆解 base = estimated_deduction × 14400 / late_minutes，加上 row 內
employee_name/employee_number 即還原全員底薪。supervisor（主管）角色持
ATTENDANCE_READ 但不持 SALARY_READ，等於從低權限旁路看到最敏感薪資欄。

修補：金額欄綁「全員薪資視野」(has_full_salary_view = admin/hr) 而非考勤可見度。
非 admin/hr 的 estimated_deduction 一律遮罩為 None（與 salary_access.py 既有
「跨員工彙總金額需 admin/hr」口徑一致）；遲到分鐘等出勤資訊保留。
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
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.attendance import router as attendance_router
from api.attendance.anomalies import _build_anomaly_rows
from models.base import Base
from models.attendance import Attendance, AttendanceStatus
from models.employee import Employee
from models.database import User
from utils.auth import hash_password

# late_minutes=30, base_salary=36000 → daily=1200, /8/60=2.5, ×30 = 75
EXPECTED_LATE_DEDUCTION = 75


# ───────────────────────── 單元層：_build_anomaly_rows ─────────────────────────


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


def _seed_late_anomaly(session):
    emp = Employee(
        employee_id="LK001", name="洩薪測試員", base_salary=36000, is_active=True
    )
    session.add(emp)
    session.commit()
    att = Attendance(
        employee_id=emp.id,
        attendance_date=date(2026, 5, 15),
        status=AttendanceStatus.LATE.value,
        is_late=True,
        late_minutes=30,
        punch_in_time=datetime.combine(date(2026, 5, 15), datetime.min.time()),
    )
    session.add(att)
    session.commit()


def test_build_rows_masks_deduction_when_amounts_excluded(db_session):
    """include_amounts=False（fail-closed 預設）→ 遲到金額遮罩為 None。"""
    _seed_late_anomaly(db_session)
    rows = _build_anomaly_rows(db_session, 2026, 5, "all", include_amounts=False)
    late = [r for r in rows if r["type"] == "late"]
    assert late, "應有遲到 row"
    assert late[0]["estimated_deduction"] is None


def test_build_rows_default_is_failclosed(db_session):
    """未顯式傳 include_amounts → 預設不洩金額（fail-closed）。"""
    _seed_late_anomaly(db_session)
    rows = _build_anomaly_rows(db_session, 2026, 5, "all")
    late = [r for r in rows if r["type"] == "late"]
    assert late[0]["estimated_deduction"] is None


def test_build_rows_includes_deduction_when_amounts_included(db_session):
    """include_amounts=True（admin/hr）→ 回傳真實金額。"""
    _seed_late_anomaly(db_session)
    rows = _build_anomaly_rows(db_session, 2026, 5, "all", include_amounts=True)
    late = [r for r in rows if r["type"] == "late"]
    assert late[0]["estimated_deduction"] == EXPECTED_LATE_DEDUCTION


# ───────────────────────── 端點層：授權邊界 ─────────────────────────


@pytest.fixture
def anomalies_client(tmp_path):
    db_path = tmp_path / "anomaly_leak.sqlite"
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
    app.include_router(attendance_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, *, username, role, permission_names, employee_id=None):
    user = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password("Pass1234"),
        role=role,
        permission_names=permission_names,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": "Pass1234"}
    )
    assert res.status_code == 200, res.text


def _seed_endpoint_data(session_factory):
    with session_factory() as s:
        target = Employee(
            employee_id="LKE01",
            name="被洩員工",
            base_salary=36000,
            hire_date=date(2024, 1, 1),
            is_active=True,
        )
        viewer_emp = Employee(
            employee_id="LKE02",
            name="主管本人",
            base_salary=40000,
            hire_date=date(2024, 1, 1),
            is_active=True,
        )
        s.add_all([target, viewer_emp])
        s.flush()
        s.add(
            Attendance(
                employee_id=target.id,
                attendance_date=date(2026, 5, 15),
                status=AttendanceStatus.LATE.value,
                is_late=True,
                late_minutes=30,
                punch_in_time=datetime.combine(date(2026, 5, 15), datetime.min.time()),
            )
        )
        _create_user(
            s,
            username="sv_leak",
            role="supervisor",
            permission_names=["ATTENDANCE_READ"],
            employee_id=viewer_emp.id,
        )
        _create_user(s, username="adm_leak", role="admin", permission_names=["*"])
        s.commit()


def _late_item(payload):
    items = payload["items"]
    late = [i for i in items if i["type"] == "late"]
    assert late, f"回應應含遲到 row: {items}"
    return late[0]


def test_supervisor_cannot_see_salary_derived_deduction(anomalies_client):
    """supervisor（ATTENDANCE_READ，無 SALARY_READ）→ 金額遮罩，不可逆推底薪。"""
    client, sf = anomalies_client
    _seed_endpoint_data(sf)
    _login(client, "sv_leak")
    res = client.get("/api/attendance/anomalies?year=2026&month=5")
    assert res.status_code == 200, res.text
    assert _late_item(res.json())["estimated_deduction"] is None


def test_admin_sees_real_deduction(anomalies_client):
    """admin（全員薪資視野）→ 看到真實金額。"""
    client, sf = anomalies_client
    _seed_endpoint_data(sf)
    _login(client, "adm_leak")
    res = client.get("/api/attendance/anomalies?year=2026&month=5")
    assert res.status_code == 200, res.text
    assert _late_item(res.json())["estimated_deduction"] == EXPECTED_LATE_DEDUCTION
