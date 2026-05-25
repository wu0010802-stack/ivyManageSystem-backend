"""驗證 POST / DELETE magic-link endpoints。"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.offboarding import router as offboarding_router
from models.database import Base, Employee, User, LeaveQuota
from utils.auth import hash_password

_counter = 0


@pytest.fixture
def integrated_client(tmp_path):
    """client + session_factory 一起回傳，供需要同時操作 HTTP + DB 的 test 使用。"""
    db_path = tmp_path / "ml-integrated.sqlite"
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
    app.include_router(offboarding_router, prefix="/api")

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_admin_user(session_factory, username="ml_admin", password="AdminPass123"):
    """在 DB 建立 admin 帳號並回傳 (username, password)。"""
    with session_factory() as session:
        session.add(
            User(
                employee_id=None,
                username=username,
                password_hash=hash_password(password),
                role="admin",
                permission_names=["*"],
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()
    return username, password


@pytest.fixture
def admin_login(integrated_client):
    """回傳一個可呼叫的 helper，每次呼叫均登入並取得 cookie headers。"""
    client, sf = integrated_client
    username, password = _seed_admin_user(sf, username="ml_admin_login")

    def _login():
        r = client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        assert r.status_code == 200, f"admin_login 失敗：{r.text}"
        # TestClient 自動保留 session cookie；回傳空 headers（cookie 已在 client）
        return {}

    return _login


@pytest.fixture
def employee_factory(integrated_client):
    """建立測試員工；需在 integrated_client 已建立的 DB 中操作。"""
    client, sf = integrated_client

    def _factory(
        *,
        name: str = None,
        hire_date=date(2020, 1, 1),
        is_active: bool = True,
        daily_wage: float = None,
    ) -> Employee:
        global _counter
        _counter += 1
        base_salary = int(daily_wage * 30) if daily_wage is not None else 0
        with sf() as session:
            emp = Employee(
                employee_id=f"ML{_counter:04d}",
                name=name or f"魔法連結員工{_counter}",
                hire_date=hire_date,
                is_active=is_active,
                base_salary=base_salary,
            )
            session.add(emp)
            session.commit()
            session.refresh(emp)
            return emp

    return _factory


@pytest.fixture
def leave_quota_factory(integrated_client):
    """建立 leave quota。"""
    client, sf = integrated_client

    def _factory(
        *,
        employee_id: int,
        year: int,
        leave_type: str,
        total_hours: float,
    ) -> LeaveQuota:
        with sf() as session:
            quota = LeaveQuota(
                employee_id=employee_id,
                year=year,
                leave_type=leave_type,
                total_hours=total_hours,
            )
            session.add(quota)
            session.commit()
            session.refresh(quota)
            return quota

    return _factory


def _process_offboarding(client, admin_login, emp_id):
    """helper: 先建 offboarding record（後續測 magic-link 才有 record 可用）。"""
    headers = admin_login()
    return client.post(
        f"/api/offboarding/{emp_id}/process",
        json={"resign_date": "2026-06-15", "resign_reason": "test"},
        headers=headers,
    )


def test_post_magic_link_returns_plaintext_token(
    integrated_client,
    admin_login,
    employee_factory,
    leave_quota_factory,
):
    client, _ = integrated_client
    emp = employee_factory(daily_wage=1800)
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    r = _process_offboarding(client, admin_login, emp.id)
    assert r.status_code == 200, f"process 失敗：{r.text}"

    headers = admin_login()
    response = client.post(
        f"/api/offboarding/{emp.id}/magic-link",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["employee_id"] == emp.id
    assert "token" in body and len(body["token"]) >= 40
    assert "expires_at" in body
    assert body["download_url"] == f"/api/offboarding/download?token={body['token']}"


def test_post_magic_link_404_when_no_record(
    integrated_client,
    admin_login,
    employee_factory,
):
    client, _ = integrated_client
    emp = employee_factory()
    headers = admin_login()
    response = client.post(
        f"/api/offboarding/{emp.id}/magic-link",
        headers=headers,
    )
    assert response.status_code == 404


def test_delete_magic_link_revokes_token(
    integrated_client,
    admin_login,
    employee_factory,
    leave_quota_factory,
):
    client, _ = integrated_client
    emp = employee_factory(daily_wage=1800)
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    r = _process_offboarding(client, admin_login, emp.id)
    assert r.status_code == 200, f"process 失敗：{r.text}"

    headers = admin_login()
    client.post(f"/api/offboarding/{emp.id}/magic-link", headers=headers)
    r = client.delete(f"/api/offboarding/{emp.id}/magic-link", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["employee_id"] == emp.id

    # GET detail → magic_link_active False
    detail = client.get(f"/api/offboarding/{emp.id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["magic_link_active"] is False


def test_repost_magic_link_overwrites_previous(
    integrated_client,
    admin_login,
    employee_factory,
    leave_quota_factory,
):
    client, _ = integrated_client
    emp = employee_factory(daily_wage=1800)
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    r = _process_offboarding(client, admin_login, emp.id)
    assert r.status_code == 200, f"process 失敗：{r.text}"

    headers = admin_login()
    r1 = client.post(f"/api/offboarding/{emp.id}/magic-link", headers=headers)
    r2 = client.post(f"/api/offboarding/{emp.id}/magic-link", headers=headers)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["token"] != r2.json()["token"]
