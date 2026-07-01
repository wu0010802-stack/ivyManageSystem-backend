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
