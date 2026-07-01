"""kiosk punch 端點測試：即時寫入、server now 鎖死、PIN 驗證。"""

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
from models.database import Attendance, Employee
from utils.auth import hash_password
from utils.kiosk_guard import assert_kiosk_ip_allowed
from utils.rate_limit import reset_in_memory_limiters


@pytest.fixture
def kiosk_app_client(tmp_path):
    """最小 FastAPI app（無 CSRF 中介層），掛 attendance router（含 kiosk）。"""
    db_path = tmp_path / "kiosk_punch.sqlite"
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


def _create_emp(sf, eid="E980", pin="1234") -> int:
    """員工建立 helper，回傳 employee.id（session 關前取值，避免 DetachedInstanceError）。"""
    with sf() as session:
        e = Employee(
            employee_id=eid,
            name="李老師",
            work_start_time="08:00",
            work_end_time="17:00",
            is_active=True,
            punch_pin_hash=hash_password(pin),
        )
        session.add(e)
        session.commit()
        session.refresh(e)
        return e.id


def test_punch_writes_punch_in(kiosk_app_client):
    """第一次打卡寫入 punch_in_time，source='kiosk'，回傳 action='punch_in'。"""
    client, sf = kiosk_app_client
    emp_id = _create_emp(sf)
    res = client.post(
        "/api/attendance/kiosk/punch", json={"employee_id": emp_id, "pin": "1234"}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["action"] == "punch_in"
    assert data["employee_name"] == "李老師"

    with sf() as session:
        row = session.query(Attendance).filter(Attendance.employee_id == emp_id).one()
        assert row.punch_in_time is not None
        assert row.source == "kiosk"


def test_punch_second_is_punch_out(kiosk_app_client):
    """第二次打卡同日 → action='punch_out'。"""
    client, sf = kiosk_app_client
    emp_id = _create_emp(sf, eid="E981")
    client.post(
        "/api/attendance/kiosk/punch", json={"employee_id": emp_id, "pin": "1234"}
    )
    res = client.post(
        "/api/attendance/kiosk/punch", json={"employee_id": emp_id, "pin": "1234"}
    )
    assert res.status_code == 200
    assert res.json()["action"] == "punch_out"


def test_punch_body_ignores_any_timestamp_field(kiosk_app_client):
    """self 反向鎖死：body 夾帶 punch_in_time 欄位被 schema 忽略，實際寫入為 server now。"""
    client, sf = kiosk_app_client
    emp_id = _create_emp(sf, eid="E982")
    res = client.post(
        "/api/attendance/kiosk/punch",
        json={
            "employee_id": emp_id,
            "pin": "1234",
            "punch_in_time": "2020-01-01T00:00:00",  # 應被忽略
        },
    )
    assert res.status_code == 200
    with sf() as session:
        row = session.query(Attendance).filter(Attendance.employee_id == emp_id).one()
        # server now 的年份一定不是 2020
        assert row.punch_in_time is not None
        assert row.punch_in_time.year != 2020


def test_punch_wrong_pin_401(kiosk_app_client):
    """PIN 錯誤 → 401。"""
    client, sf = kiosk_app_client
    emp_id = _create_emp(sf, eid="E983")
    res = client.post(
        "/api/attendance/kiosk/punch", json={"employee_id": emp_id, "pin": "0000"}
    )
    assert res.status_code == 401


def test_punch_response_shape(kiosk_app_client):
    """回傳欄位：employee_name、action、punch_time、status 皆存在。"""
    client, sf = kiosk_app_client
    emp_id = _create_emp(sf, eid="E984")
    res = client.post(
        "/api/attendance/kiosk/punch", json={"employee_id": emp_id, "pin": "1234"}
    )
    assert res.status_code == 200
    data = res.json()
    for field in ("employee_name", "action", "punch_time", "status"):
        assert field in data, f"回傳缺少欄位 {field}"


def test_punch_rate_limit_locks_after_repeated_failures(kiosk_app_client):
    """連續 PIN 錯誤超過限流門檻 → 429（不同 employee_id 隔離）。"""
    client, sf = kiosk_app_client
    emp_id = _create_emp(sf, eid="E985")
    last = None
    for _ in range(8):
        last = client.post(
            "/api/attendance/kiosk/punch",
            json={"employee_id": emp_id, "pin": "0000"},
        )
    assert last.status_code == 429
