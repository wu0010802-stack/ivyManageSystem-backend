"""kiosk preview 端點測試：PIN 驗證、限流。"""

import os
import sys

import models.base as base_module
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.attendance import router as attendance_router
from models.base import Base
from models.database import Employee
from utils.auth import hash_password
from utils.kiosk_guard import assert_kiosk_ip_allowed
from utils.rate_limit import reset_in_memory_limiters


@pytest.fixture
def kiosk_app_client(tmp_path):
    """最小 FastAPI app（無 CSRF 中介層），掛 attendance router（含 kiosk）。"""
    db_path = tmp_path / "kiosk_preview.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    reset_in_memory_limiters()

    _app = FastAPI()
    _app.include_router(attendance_router)
    _app.dependency_overrides[assert_kiosk_ip_allowed] = lambda: None

    with TestClient(_app) as client:
        yield client, session_factory

    _app.dependency_overrides.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_emp(sf, eid="E970", pin="1234") -> int:
    """員工建立 helper，回傳 employee.id（session 關前取值，避免 DetachedInstanceError）。"""
    with sf() as session:
        e = Employee(
            employee_id=eid,
            name="王老師",
            work_start_time="08:00",
            work_end_time="17:00",
            is_active=True,
            punch_pin_hash=hash_password(pin),
        )
        session.add(e)
        session.commit()
        session.refresh(e)
        return e.id


def test_preview_correct_pin_returns_punch_in(kiosk_app_client):
    client, sf = kiosk_app_client
    emp_id = _create_emp(sf)
    res = client.post(
        "/api/attendance/kiosk/preview", json={"employee_id": emp_id, "pin": "1234"}
    )
    assert res.status_code == 200
    assert res.json()["action"] == "punch_in"
    assert res.json()["employee_name"] == "王老師"


def test_preview_wrong_pin_401(kiosk_app_client):
    client, sf = kiosk_app_client
    emp_id = _create_emp(sf)
    res = client.post(
        "/api/attendance/kiosk/preview", json={"employee_id": emp_id, "pin": "9999"}
    )
    assert res.status_code == 401


def test_preview_no_pin_set_400(kiosk_app_client):
    client, sf = kiosk_app_client
    with sf() as session:
        e = Employee(employee_id="E971", name="無PIN", is_active=True)
        session.add(e)
        session.commit()
        session.refresh(e)
        eid = e.id
    res = client.post(
        "/api/attendance/kiosk/preview", json={"employee_id": eid, "pin": "1234"}
    )
    assert res.status_code == 400


def test_preview_rate_limit_locks_after_repeated_failures(kiosk_app_client):
    client, sf = kiosk_app_client
    emp_id = _create_emp(sf, eid="E972")
    last = None
    for _ in range(8):
        last = client.post(
            "/api/attendance/kiosk/preview",
            json={"employee_id": emp_id, "pin": "0000"},
        )
    assert last.status_code == 429  # 連續失敗後鎖定


# ──────────────────────────────────────────────
# I1：未知員工 → 404
# ──────────────────────────────────────────────
def test_preview_unknown_employee_404(kiosk_app_client):
    """I1：傳入不存在的 employee_id → 404（_authenticate_pin 查不到在職員工）。"""
    client, sf = kiosk_app_client
    res = client.post(
        "/api/attendance/kiosk/preview",
        json={"employee_id": 99999, "pin": "1234"},
    )
    assert res.status_code == 404


# ──────────────────────────────────────────────
# I2：成功驗 PIN 不計入失敗配額（核心安全語義回歸）
# ──────────────────────────────────────────────
def test_preview_success_does_not_consume_fail_quota(kiosk_app_client):
    """I2：連錯 4 次 → 正確 PIN 應 200（成功不消耗失敗配額）→ 再錯一次應 401（非 429）。"""
    client, sf = kiosk_app_client
    emp_id = _create_emp(sf, eid="E974", pin="5678")
    # 連錯 4 次：失敗計數累積至 4
    for i in range(4):
        res = client.post(
            "/api/attendance/kiosk/preview",
            json={"employee_id": emp_id, "pin": "0000"},
        )
        assert (
            res.status_code == 401
        ), f"第 {i + 1} 次錯誤應為 401，實為 {res.status_code}"
    # 第 5 次送正確 PIN → 200；_pin_fail_limiter.check() 不會被呼叫，計數不推進
    res = client.post(
        "/api/attendance/kiosk/preview",
        json={"employee_id": emp_id, "pin": "5678"},
    )
    assert res.status_code == 200, f"正確 PIN 應 200，實為 {res.status_code}"
    # 再錯一次：計數從 4 推至 5，仍 < 限流門檻 → 401（非 429）
    res = client.post(
        "/api/attendance/kiosk/preview",
        json={"employee_id": emp_id, "pin": "0000"},
    )
    assert (
        res.status_code == 401
    ), f"成功後第一次錯誤應仍 401（非 429），實為 {res.status_code}"


# ──────────────────────────────────────────────
# M1：限流邊界精確（第 5 次錯仍 401，第 6 次才 429）
# ──────────────────────────────────────────────
def test_preview_rate_limit_boundary_5th_401_6th_429(kiosk_app_client):
    """M1：max_calls=5 — 第 1–5 次錯誤均 401，第 6 次才 429。"""
    client, sf = kiosk_app_client
    emp_id = _create_emp(sf, eid="E975", pin="4321")
    # 前 5 次錯誤：每次 check() 呼叫時 len(timestamps) < 5，允許通過後 401
    for i in range(5):
        res = client.post(
            "/api/attendance/kiosk/preview",
            json={"employee_id": emp_id, "pin": "0000"},
        )
        assert (
            res.status_code == 401
        ), f"第 {i + 1} 次錯誤應為 401，實為 {res.status_code}"
    # 第 6 次：len(timestamps) == 5 >= max_calls → 429
    res = client.post(
        "/api/attendance/kiosk/preview",
        json={"employee_id": emp_id, "pin": "0000"},
    )
    assert res.status_code == 429, f"第 6 次錯誤應為 429，實為 {res.status_code}"


# ──────────────────────────────────────────────
# M2：PIN 格式非法 → 422（field_validator）
# ──────────────────────────────────────────────
def test_preview_invalid_pin_format_422(kiosk_app_client):
    """M2：PIN 3 位數或含字母 → 422（KioskPunchRequest._valid_pin field_validator）。"""
    client, sf = kiosk_app_client
    # 3 位數（太短）
    res = client.post(
        "/api/attendance/kiosk/preview",
        json={"employee_id": 1, "pin": "123"},
    )
    assert res.status_code == 422, f"3 位 PIN 應 422，實為 {res.status_code}"
    # 含字母
    res = client.post(
        "/api/attendance/kiosk/preview",
        json={"employee_id": 1, "pin": "12ab"},
    )
    assert res.status_code == 422, f"含字母 PIN 應 422，實為 {res.status_code}"
