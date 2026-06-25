"""rank 10 回歸：同一天無法登錄多筆部分請假（attendance 一天一列、單一 leave_record_id）。

重疊服務對「同日、時段不重疊」的兩筆單日部分假以時段精比放行，但核准第二筆時
sync.apply 撞 LeaveAttendanceConflict → 422（業務允許、系統結構做不到）。改在建立時
就擋下並給可操作訊息。
"""

import os
import sys
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.leaves as leaves_module
import api.overtimes as overtimes_module
import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.leaves import router as leaves_router
from models.database import Base, Employee, LeaveRecord, User
from utils.auth import hash_password


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sdp.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    sf = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine, base_module._SessionFactory = engine, sf
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    monkeypatch.setattr(leaves_module, "_salary_engine", MagicMock())
    monkeypatch.setattr(overtimes_module, "_salary_engine", MagicMock())

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(leaves_router)
    with TestClient(app) as client:
        with sf() as s:
            s.add(
                Employee(
                    employee_id="E001", name="員工", base_salary=36000, is_active=True
                )
            )
            s.add(
                User(
                    username="hr",
                    password_hash=hash_password("AdminPass123"),
                    role="admin",
                    permission_names=["*"],
                    is_active=True,
                    must_change_password=False,
                )
            )
            s.commit()
            emp_id = s.query(Employee).first().id
        assert (
            client.post(
                "/api/auth/login", json={"username": "hr", "password": "AdminPass123"}
            ).status_code
            == 200
        )
        yield client, emp_id

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine, base_module._SessionFactory = old_e, old_sf
    engine.dispose()


def _post_partial(client, emp_id, d, st, et):
    return client.post(
        "/api/leaves",
        json={
            "employee_id": emp_id,
            "leave_type": "personal",
            "start_date": d,
            "end_date": d,
            "leave_hours": 4,
            "start_time": st,
            "end_time": et,
        },
    )


def test_second_same_day_partial_leave_blocked_at_create(app_client):
    client, emp_id = app_client
    r1 = _post_partial(client, emp_id, "2026-09-15", "08:00", "12:00")
    assert r1.status_code in (200, 201), f"第一筆應成功；{r1.json()}"

    r2 = _post_partial(client, emp_id, "2026-09-15", "13:00", "17:00")
    assert (
        r2.status_code == 409
    ), f"同日第二筆部分假應被擋；{r2.status_code} {r2.json()}"
    assert "部分請假" in r2.json().get("detail", "") or "同一天" in r2.json().get(
        "detail", ""
    )


def test_partial_leave_on_different_day_allowed(app_client):
    """控制案例：不同天的部分假不受影響。"""
    client, emp_id = app_client
    assert _post_partial(
        client, emp_id, "2026-09-15", "08:00", "12:00"
    ).status_code in (200, 201)
    assert _post_partial(
        client, emp_id, "2026-09-16", "08:00", "12:00"
    ).status_code in (200, 201)
