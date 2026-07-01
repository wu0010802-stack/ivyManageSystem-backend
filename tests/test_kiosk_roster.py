# tests/test_kiosk_roster.py
"""kiosk roster 端點測試：名單最小揭露 + IP 守衛。"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import app
from utils.kiosk_guard import assert_kiosk_ip_allowed
from models.database import Employee
from utils.auth import hash_password


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
