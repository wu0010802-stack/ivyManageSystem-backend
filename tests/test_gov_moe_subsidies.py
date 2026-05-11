"""tests/test_gov_moe_subsidies.py — 特教加給/助理鐘點費 endpoint tests."""

import os
import sys
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.gov_moe import router as gov_moe_router
from models.base import Base
from models.database import User
from models.employee import Employee  # noqa: F401 — registers employees table
from models.gov_moe import (
    SpecialEducationSubsidy,
)  # noqa: F401 — registers table on Base
from utils.auth import hash_password

# ---------------------------------------------------------------------------
# Fixtures (copied verbatim from test_gov_moe_certificates.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def gov_moe_client(tmp_path):
    db_path = tmp_path / "gov_moe.sqlite"
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
    app.include_router(gov_moe_router, prefix="/api")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


# ---------------------------------------------------------------------------
# Auth helper (copied verbatim from Sub-system C tests)
# ---------------------------------------------------------------------------


def _login_admin(client, session_factory):
    with session_factory() as s:
        s.add(
            User(
                username="admin",
                password_hash=hash_password("AdminPass1"),
                role="admin",
                permissions=-1,
                is_active=True,
            )
        )
        s.commit()
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "AdminPass1"}
    )
    return resp.json().get("access_token") or resp.cookies.get("access_token")


# ---------------------------------------------------------------------------
# B1 test: list returns empty list
# ---------------------------------------------------------------------------


def test_subsidies_list_returns_empty(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    r = client.get(
        "/api/gov-moe/subsidies",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    assert r.json() == []


# ---------------------------------------------------------------------------
# B2 helpers
# ---------------------------------------------------------------------------

_seed_counter = 0


def _seed_employee(session_factory):
    global _seed_counter
    _seed_counter += 1
    with session_factory() as s:
        e = Employee(
            employee_id=f"T{_seed_counter:04d}",
            name="陳老師",
            is_active=True,
        )
        s.add(e)
        s.commit()
        s.refresh(e)
        return e.id


def _create_subsidy(client, token, eid):
    return client.post(
        "/api/gov-moe/subsidies",
        json={
            "subsidy_type": "teacher_extra",
            "employee_id": eid,
            "period_start": "2026-05-01",
            "period_end": "2026-05-31",
            "amount_requested": "3000.00",
        },
        headers={"Authorization": f"Bearer {token}"},
    )


# ---------------------------------------------------------------------------
# B2 tests: full state-machine + RBAC
# ---------------------------------------------------------------------------


def test_subsidy_create_starts_in_draft(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    eid = _seed_employee(sf)
    r = _create_subsidy(client, tok, eid)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "draft"


def test_subsidy_full_state_flow(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    eid = _seed_employee(sf)
    auth = {"Authorization": f"Bearer {tok}"}
    sid = _create_subsidy(client, tok, eid).json()["id"]

    r = client.put(f"/api/gov-moe/subsidies/{sid}/submit", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted"

    r = client.put(
        f"/api/gov-moe/subsidies/{sid}/approve",
        json={"amount_approved": "2500.00"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    # Money 欄位透過 Money TypeDecorator 回傳 float，Pydantic 序列化為 Decimal 字串
    # 使用 Decimal 值比較，忽略尾數格式差異
    assert Decimal(str(r.json()["amount_approved"])) == Decimal("2500.00")

    r = client.put(
        f"/api/gov-moe/subsidies/{sid}/mark_paid",
        json={"paid_at": "2026-06-15T10:00:00"},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "paid"


def test_subsidy_cannot_skip_state(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    eid = _seed_employee(sf)
    auth = {"Authorization": f"Bearer {tok}"}
    sid = _create_subsidy(client, tok, eid).json()["id"]
    # draft → mark_paid 應拒絕（須先 approve，approve 前須 submit）
    r = client.put(
        f"/api/gov-moe/subsidies/{sid}/mark_paid",
        json={"paid_at": "2026-06-15T10:00:00"},
        headers=auth,
    )
    assert r.status_code == 409


def test_subsidy_reject_submitted(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    eid = _seed_employee(sf)
    auth = {"Authorization": f"Bearer {tok}"}
    sid = _create_subsidy(client, tok, eid).json()["id"]
    client.put(f"/api/gov-moe/subsidies/{sid}/submit", headers=auth)
    r = client.put(f"/api/gov-moe/subsidies/{sid}/reject", headers=auth)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"


def test_subsidy_period_validation(gov_moe_client):
    client, sf = gov_moe_client
    tok = _login_admin(client, sf)
    eid = _seed_employee(sf)
    r = client.post(
        "/api/gov-moe/subsidies",
        json={
            "subsidy_type": "teacher_extra",
            "employee_id": eid,
            "period_start": "2026-06-30",
            "period_end": "2026-06-01",
            "amount_requested": "0",
        },
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 422


def test_teacher_cannot_create_subsidy(gov_moe_client):
    client, sf = gov_moe_client
    eid = _seed_employee(sf)
    # 先建立教師帳號，不用 admin 登入
    with sf() as s:
        s.add(
            User(
                username="t_teacher",
                password_hash=hash_password("Teach123"),
                role="teacher",
                permissions=0,
                is_active=True,
            )
        )
        s.commit()
    resp = client.post(
        "/api/auth/login",
        json={"username": "t_teacher", "password": "Teach123"},
    )
    tok = resp.json().get("access_token") or resp.cookies.get("access_token")
    r = _create_subsidy(client, tok, eid)
    assert r.status_code == 403
