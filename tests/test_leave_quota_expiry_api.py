"""tests/test_leave_quota_expiry_api.py — 4 HR API endpoints 的整合測試。

Self-contained：自行建立 SQLite in-memory DB + FastAPI app，不依賴全域 conftest fixtures。
auth_router 用於 cookie 登入；leave_quota_expiry router 為受測目標。
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
from models.database import Base, Employee, User
from utils.auth import hash_password

# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

_EMP_COUNTER = 0


@pytest.fixture
def app_client(tmp_path):
    """self-contained SQLite DB + FastAPI app。

    Yields:
        (client, session_factory)
    """
    from api.leave_quota_expiry import router as lqe_router

    db_path = tmp_path / "lqe-api.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
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
    app.include_router(lqe_router, prefix="/api")

    with TestClient(app, raise_server_exceptions=True) as client:
        yield client, sf

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed_user(sf, username: str, password: str, permissions: list[str]) -> None:
    """在 SQLite DB 建立帳號。"""
    with sf() as session:
        u = User(
            employee_id=None,
            username=username,
            password_hash=hash_password(password),
            role="admin",
            permission_names=permissions,
            is_active=True,
            must_change_password=False,
        )
        session.add(u)
        session.commit()


def _login(client: TestClient, username: str, password: str) -> None:
    """透過 auth_router 登入；TestClient 自動儲存 cookie。"""
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, f"登入失敗：{r.text}"


def _make_employee(
    sf, *, hire_date: date = date(2020, 1, 1), name: str = None
) -> Employee:
    """建立測試員工並回傳（已含 DB auto-id）。"""
    global _EMP_COUNTER
    _EMP_COUNTER += 1
    with sf() as session:
        emp = Employee(
            employee_id=f"LQE{_EMP_COUNTER:04d}",
            name=name or f"測試員工{_EMP_COUNTER}",
            hire_date=hire_date,
            is_active=True,
            base_salary=36000,
            employee_type="regular",
        )
        session.add(emp)
        session.commit()
        session.refresh(emp)
        return emp


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_upcoming_lists_grants_within_window(app_client):
    """GET /leave-quota-expiry/upcoming?days=30 列即將到期 active grant。"""
    from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
    from models.overtime import OvertimeRecord

    client, sf = app_client
    _seed_user(sf, "hr1", "Passw0rd!", ["LEAVES_READ"])
    _login(client, "hr1", "Passw0rd!")

    emp = _make_employee(sf)

    # 建一筆 overtime_record 作為 FK
    with sf() as session:
        ot = OvertimeRecord(
            employee_id=emp.id,
            overtime_date=date.today() - timedelta(days=355),
            overtime_type="weekday",
            hours=4.0,
            is_approved=True,
        )
        session.add(ot)
        session.flush()

        # 10 天後到期（在 30 天 window 內）
        grant_in = OvertimeCompLeaveGrant(
            overtime_record_id=ot.id,
            employee_id=emp.id,
            granted_hours=4.0,
            granted_at=date.today() - timedelta(days=355),
            expires_at=date.today() + timedelta(days=10),
            status="active",
        )
        session.add(grant_in)

        # 60 天後到期（不在 window 內）
        ot2 = OvertimeRecord(
            employee_id=emp.id,
            overtime_date=date.today() - timedelta(days=305),
            overtime_type="weekday",
            hours=4.0,
            is_approved=True,
        )
        session.add(ot2)
        session.flush()

        grant_out = OvertimeCompLeaveGrant(
            overtime_record_id=ot2.id,
            employee_id=emp.id,
            granted_hours=4.0,
            granted_at=date.today() - timedelta(days=305),
            expires_at=date.today() + timedelta(days=60),
            status="active",
        )
        session.add(grant_out)
        session.commit()
        session.refresh(grant_in)
        in_id = grant_in.id

    resp = client.get("/api/leave-quota-expiry/upcoming?days=30")
    assert resp.status_code == 200
    data = resp.json()
    assert "grants" in data
    assert len(data["grants"]) == 1
    assert data["grants"][0]["grant_id"] == in_id


def test_anniversaries_lists_upcoming_30_days(app_client):
    """GET /leave-quota-expiry/anniversaries 列未來 30 天內滿週年的員工。"""
    client, sf = app_client
    _seed_user(sf, "hr2", "Passw0rd!", ["LEAVES_READ"])
    _login(client, "hr2", "Passw0rd!")

    today = date.today()
    # 在職員工：週年在 5 天後（hire_date 年份早 2 年，月日 = today+5）
    anniversary_day = today + timedelta(days=5)
    hire_date = anniversary_day.replace(year=anniversary_day.year - 2)
    emp_match = _make_employee(sf, hire_date=hire_date, name="週年員工")

    # 在職員工：週年在 60 天後（不在 window）
    far_day = today + timedelta(days=60)
    hire_far = far_day.replace(year=far_day.year - 1)
    _make_employee(sf, hire_date=hire_far, name="遠期員工")

    resp = client.get("/api/leave-quota-expiry/anniversaries?days=30")
    assert resp.status_code == 200
    data = resp.json()
    assert "anniversaries" in data
    emp_ids = [a["employee_id"] for a in data["anniversaries"]]
    assert emp_match.id in emp_ids


def test_payout_history_returns_logs(app_client):
    """GET /leave-quota-expiry/payout-history 回傳 payout log 列表。"""
    from decimal import Decimal
    from models.unused_leave_payout_log import UnusedLeavePayoutLog

    client, sf = app_client
    _seed_user(sf, "sal1", "Passw0rd!", ["SALARY_READ"])
    _login(client, "sal1", "Passw0rd!")

    emp = _make_employee(sf)
    with sf() as session:
        log = UnusedLeavePayoutLog(
            employee_id=emp.id,
            source_type="comp_grant_expiry",
            source_ref_id=None,
            hours=4.0,
            hourly_wage=Decimal("150.00"),
            amount=Decimal("600.00"),
            wage_basis_date=date.today(),
            salary_record_id=None,
            salary_period_year=date.today().year,
            salary_period_month=date.today().month,
            meta={},
        )
        session.add(log)
        session.commit()
        session.refresh(log)
        log_id = log.id

    resp = client.get("/api/leave-quota-expiry/payout-history?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert "logs" in data
    assert len(data["logs"]) >= 1
    assert data["logs"][0]["log_id"] == log_id
    assert data["logs"][0]["source_type"] == "comp_grant_expiry"


def test_run_now_triggers_scheduler_returns_summaries(app_client):
    """POST /leave-quota-expiry/run-now 執行 scheduler 並回傳 comp_summary + cutover_summary。"""
    client, sf = app_client
    _seed_user(sf, "sal2", "Passw0rd!", ["SALARY_WRITE"])
    _login(client, "sal2", "Passw0rd!")

    resp = client.post("/api/leave-quota-expiry/run-now")
    assert resp.status_code == 200
    data = resp.json()
    assert "comp_summary" in data
    assert "cutover_summary" in data


def test_run_now_returns_409_on_concurrent_lock_holder(app_client):
    """POST /leave-quota-expiry/run-now 當另一 worker 已持有 lock 時回傳 409 Conflict。

    模擬：第一個 session 取得 today 的 advisory lock → 第二個 request 來時
    try_scheduler_lock yield False → endpoint 回 409。
    """
    from unittest.mock import patch, MagicMock

    client, sf = app_client
    _seed_user(sf, "sal3", "Passw0rd!", ["SALARY_WRITE"])
    _login(client, "sal3", "Passw0rd!")

    # Mock try_scheduler_lock 回 acquired=False（模擬 lock 被佔）
    with patch("api.leave_quota_expiry.try_scheduler_lock") as mock_lock:
        mock_context = MagicMock()
        mock_context.__enter__ = MagicMock(return_value=False)  # acquired=False
        mock_context.__exit__ = MagicMock(return_value=None)
        mock_lock.return_value = mock_context

        resp = client.post("/api/leave-quota-expiry/run-now")
        assert resp.status_code == 409
        data = resp.json()
        assert "already running" in data["detail"].lower()
