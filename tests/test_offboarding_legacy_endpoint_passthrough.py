"""驗證舊 POST /employees/{id}/offboard 改為 passthrough 呼叫 orchestrator。

Task 13 passthrough test：
- test_legacy_resign_endpoint_creates_offboarding_record：
  呼叫舊 endpoint 後，DB 需建有 EmployeeOffboardingRecord（新行為），
  且 Employee.is_active 仍被設 False（向後相容）。
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.employees import router as employees_router
from models.database import Base, Employee, User, LeaveQuota
from models.offboarding import EmployeeOffboardingRecord
from utils.auth import hash_password

_counter = 0


@pytest.fixture
def integrated_legacy(tmp_path):
    """SQLite in-memory + TestClient（auth + employees router）。"""
    db_path = tmp_path / "offboard-legacy.sqlite"
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

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(employees_router)

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_admin(sf, username="leg_admin"):
    with sf() as s:
        s.add(
            User(
                employee_id=None,
                username=username,
                password_hash=hash_password("AdminPass1!"),
                role="admin",
                permission_names=["*"],
                is_active=True,
                must_change_password=False,
            )
        )
        s.commit()
    return username, "AdminPass1!"


def _seed_emp_with_quota(sf, *, daily_wage=1800):
    global _counter
    _counter += 1
    with sf() as s:
        emp = Employee(
            employee_id=f"LEG{_counter:04d}",
            name=f"遺留端點員工{_counter}",
            hire_date=date(2020, 1, 1),
            is_active=True,
            base_salary=int(daily_wage * 30),
        )
        s.add(emp)
        s.flush()
        quota = LeaveQuota(
            employee_id=emp.id,
            year=2026,
            leave_type="annual",
            total_hours=80.0,
        )
        s.add(quota)
        s.commit()
        return emp.id


def test_legacy_resign_endpoint_creates_offboarding_record(integrated_legacy):
    """舊 endpoint 改 passthrough：建立 EmployeeOffboardingRecord + leave snapshot，行為向後相容。

    resign_date = today，確保 is_active 被設為 False（離職日 <= today 規則）。
    """
    c, sf = integrated_legacy
    username, password = _seed_admin(sf)
    emp_id = _seed_emp_with_quota(sf, daily_wage=1800)

    login_res = c.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert login_res.status_code == 200, login_res.text

    today_str = date.today().isoformat()
    res = c.post(
        f"/api/employees/{emp_id}/offboard",
        json={"resign_date": today_str, "resign_reason": "個人因素"},
    )
    assert res.status_code == 200, res.text
    body = res.json()

    # 向後相容 response shape：離職日 = today → is_active=False
    assert body["is_active"] is False
    assert body["resign_date"] == today_str
    assert "user_account_revoked" in body

    # 新行為：建 EmployeeOffboardingRecord
    with sf() as s:
        record = (
            s.query(EmployeeOffboardingRecord).filter_by(employee_id=emp_id).first()
        )
        assert (
            record is not None
        ), "orchestrator passthrough 後應建立 EmployeeOffboardingRecord"
        assert record.resign_date.isoformat() == today_str
        assert record.leave_balance_snapshot is not None, "snapshot_leave step 應已執行"


def test_legacy_endpoint_response_shape_backward_compatible(integrated_legacy):
    """response shape 保持向後相容（前端切換前必要）。"""
    c, sf = integrated_legacy
    username, password = _seed_admin(sf, username="leg_admin2")
    emp_id = _seed_emp_with_quota(sf, daily_wage=1500)

    c.post("/api/auth/login", json={"username": username, "password": password})

    res = c.post(
        f"/api/employees/{emp_id}/offboard",
        json={"resign_date": date.today().isoformat(), "resign_reason": "合約到期"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # 必要欄位
    assert "message" in body
    assert "id" in body
    assert "name" in body
    assert "resign_date" in body
    assert "resign_reason" in body
    assert "is_active" in body
    assert "user_account_revoked" in body


def test_legacy_endpoint_404_for_unknown_employee(integrated_legacy):
    """未知員工 → 404。"""
    c, sf = integrated_legacy
    username, password = _seed_admin(sf, username="leg_admin3")
    c.post("/api/auth/login", json={"username": username, "password": password})

    res = c.post(
        "/api/employees/99999/offboard",
        json={"resign_date": "2026-06-15", "resign_reason": "test"},
    )
    assert res.status_code == 404, res.text


def test_legacy_endpoint_409_already_offboarded(integrated_legacy):
    """已有離職紀錄 → 409（ALREADY_OFFBOARDED）。"""
    c, sf = integrated_legacy
    username, password = _seed_admin(sf, username="leg_admin4")
    emp_id = _seed_emp_with_quota(sf, daily_wage=1600)

    c.post("/api/auth/login", json={"username": username, "password": password})

    # 第一次離職
    res1 = c.post(
        f"/api/employees/{emp_id}/offboard",
        json={"resign_date": "2026-06-15", "resign_reason": "第一次"},
    )
    assert res1.status_code == 200, res1.text

    # 重複離職 → 409
    res2 = c.post(
        f"/api/employees/{emp_id}/offboard",
        json={"resign_date": "2026-07-01", "resign_reason": "重複"},
    )
    assert res2.status_code == 409, res2.text
