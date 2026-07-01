# tests/test_kiosk_roster.py
"""kiosk roster 端點測試：名單最小揭露 + IP 守衛。"""

import os
import sys
from datetime import datetime, date

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import app
from utils.kiosk_guard import assert_kiosk_ip_allowed
from models.database import Employee
from models.attendance import Attendance
from utils.auth import hash_password
from utils.taipei_time import today_taipei


@pytest.fixture
def client(test_db_session):  # noqa: F811
    """TestClient，共用 test_db_session 的 SQLite DB（engine 已由 test_db_session 置換）。"""
    return TestClient(app)


@pytest.fixture
def kiosk_client(client):
    # 測試時 override IP 白名單守衛（IP guard 另有專屬單元測試 Task 6）
    app.dependency_overrides[assert_kiosk_ip_allowed] = lambda: None
    yield client
    app.dependency_overrides.pop(assert_kiosk_ip_allowed, None)


def test_roster_lists_active_with_has_pin(kiosk_client, test_db_session):
    test_db_session.add(
        Employee(
            employee_id="E960",
            name="有PIN",
            is_active=True,
            punch_pin_hash=hash_password("1234"),
        )
    )
    test_db_session.add(Employee(employee_id="E961", name="無PIN", is_active=True))
    test_db_session.add(Employee(employee_id="E962", name="已離職", is_active=False))
    test_db_session.commit()

    res = kiosk_client.get("/api/attendance/kiosk/roster")
    assert res.status_code == 200
    names = {e["name"]: e for e in res.json()}
    assert "有PIN" in names and names["有PIN"]["has_pin"] is True
    assert "無PIN" in names and names["無PIN"]["has_pin"] is False
    assert "已離職" not in names  # 離職排除
    # 最小揭露：不含 PII 欄位
    assert "phone" not in names["有PIN"] and "email" not in names["有PIN"]
    assert names["無PIN"]["today_state"] == "none"


def test_roster_today_state_in_only_and_done(kiosk_client, test_db_session):
    """測試 in_only（僅上班）與 done（已下班）兩態。"""
    emp_a = Employee(employee_id="E970", name="僅打卡進", is_active=True)
    emp_b = Employee(employee_id="E971", name="已下班", is_active=True)
    test_db_session.add(emp_a)
    test_db_session.add(emp_b)
    test_db_session.flush()

    today = today_taipei()
    test_db_session.add(
        Attendance(
            employee_id=emp_a.id,
            attendance_date=today,
            punch_in_time=datetime(2026, 6, 30, 8, 0),
            punch_out_time=None,
            status="normal",
        )
    )
    test_db_session.add(
        Attendance(
            employee_id=emp_b.id,
            attendance_date=today,
            punch_in_time=datetime(2026, 6, 30, 8, 0),
            punch_out_time=datetime(2026, 6, 30, 17, 0),
            status="normal",
        )
    )
    test_db_session.commit()

    res = kiosk_client.get("/api/attendance/kiosk/roster")
    assert res.status_code == 200
    names = {e["name"]: e for e in res.json()}
    assert names["僅打卡進"]["today_state"] == "in_only"
    assert names["已下班"]["today_state"] == "done"


def test_roster_excludes_resign_date(kiosk_client, test_db_session):
    """測試 resign_date 存在時排除該員工（即使 is_active=True）。"""
    emp_active = Employee(employee_id="E980", name="在職", is_active=True)
    emp_resigned = Employee(
        employee_id="E981",
        name="有離職日期",
        is_active=True,
        resign_date=date(2020, 1, 1),
    )
    test_db_session.add(emp_active)
    test_db_session.add(emp_resigned)
    test_db_session.commit()

    res = kiosk_client.get("/api/attendance/kiosk/roster")
    assert res.status_code == 200
    names = {e["name"]: e for e in res.json()}
    assert "在職" in names
    assert "有離職日期" not in names  # 有 resign_date 即排除
