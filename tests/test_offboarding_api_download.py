"""驗證公開 GET /offboarding/download endpoint + 安全強化（Task 6）。

測試策略：
- build_offboarding_zip 以 patch 取代（避免真實 PDF/CSV 依賴）
- integrated_client fixture 沿用 test_offboarding_api_magic_link.py pattern
- 所有驗證統一 410 Gone（防 enumeration，不暴露差異原因）
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from datetime import date, datetime, timedelta
from unittest.mock import patch

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
from models.offboarding import EmployeeOffboardingRecord
from utils.auth import hash_password

_counter = 0


# ---------------------------------------------------------------------------
# 假 ZIP bytes（含 magic header，讓 response 看起來是合法 ZIP）
# ---------------------------------------------------------------------------
def _make_fake_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("certificate.pdf", b"%PDF-1.4 fake certificate")
        zf.writestr("attendance.csv", b"date,status\n2026-06-15,present")
    return buf.getvalue()


_FAKE_ZIP = _make_fake_zip()


# ---------------------------------------------------------------------------
# Fixtures（對齊 test_offboarding_api_magic_link.py）
# ---------------------------------------------------------------------------


@pytest.fixture
def integrated_client(tmp_path):
    """client + session_factory 一起回傳，供需要同時操作 HTTP + DB 的 test 使用。"""
    db_path = tmp_path / "dl-integrated.sqlite"
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


def _seed_admin_user(session_factory, username="dl_admin", password="AdminPass123"):
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
    username, password = _seed_admin_user(sf, username="dl_admin_login")

    def _login():
        r = client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        assert r.status_code == 200, f"admin_login 失敗：{r.text}"
        return {}

    return _login


@pytest.fixture
def employee_factory(integrated_client):
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
                employee_id=f"DL{_counter:04d}",
                name=name or f"下載測試員工{_counter}",
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


# ---------------------------------------------------------------------------
# Helper：走完 process + 產 magic-link，回傳 (emp, token)
# ---------------------------------------------------------------------------


def _setup_magic_link(client, admin_login, employee_factory, leave_quota_factory):
    """建立員工 → process → magic-link → 回傳 (emp, token)。

    patch generate_certificate_pdf 避免實際 PDF 依賴。
    """
    emp = employee_factory(daily_wage=1800)
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    headers = admin_login()

    with patch(
        "services.offboarding.steps.generate_certificate.generate_certificate_pdf",
        return_value=b"%PDF-1.4 fake cert",
    ):
        r = client.post(
            f"/api/offboarding/{emp.id}/process",
            json={"resign_date": "2026-06-15", "resign_reason": "test"},
            headers=headers,
        )
    assert r.status_code == 200, f"process 失敗：{r.text}"

    r = client.post(f"/api/offboarding/{emp.id}/magic-link", headers=headers)
    assert r.status_code == 200, f"magic-link 失敗：{r.text}"
    return emp, r.json()["token"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_download_with_valid_token_returns_zip(
    integrated_client,
    admin_login,
    employee_factory,
    leave_quota_factory,
):
    """有效 token → 200 + ZIP magic bytes + 正確 headers。"""
    client, _ = integrated_client
    emp, token = _setup_magic_link(
        client, admin_login, employee_factory, leave_quota_factory
    )

    with patch(
        "api.offboarding.build_offboarding_zip",
        return_value=_FAKE_ZIP,
    ):
        response = client.get(f"/api/offboarding/download?token={token}")

    assert response.status_code == 200
    assert "application/zip" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("cache-control") == "no-store"

    # ZIP magic header
    assert response.content[:4] == b"PK\x03\x04"


def test_download_increments_count_and_sets_last_used_at(
    integrated_client,
    admin_login,
    employee_factory,
    leave_quota_factory,
):
    """成功下載後 magic_link_download_count +1 且 last_used_at 有值。"""
    client, sf = integrated_client
    emp, token = _setup_magic_link(
        client, admin_login, employee_factory, leave_quota_factory
    )

    with patch(
        "api.offboarding.build_offboarding_zip",
        return_value=_FAKE_ZIP,
    ):
        client.get(f"/api/offboarding/download?token={token}")

    with sf() as session:
        record = (
            session.query(EmployeeOffboardingRecord).filter_by(employee_id=emp.id).one()
        )
        assert record.magic_link_download_count == 1
        assert record.magic_link_last_used_at is not None


def test_download_returns_410_for_invalid_token(integrated_client):
    """不存在的 token → 410 Gone（不暴露原因）。"""
    client, _ = integrated_client
    r = client.get("/api/offboarding/download?token=fake-nonexistent-token-xyz")
    assert r.status_code == 410
    assert r.json()["detail"] == "LINK_NO_LONGER_VALID"


def test_download_returns_410_for_expired_token(
    integrated_client,
    admin_login,
    employee_factory,
    leave_quota_factory,
):
    """已過期 token → 410 Gone。"""
    client, sf = integrated_client
    emp, token = _setup_magic_link(
        client, admin_login, employee_factory, leave_quota_factory
    )

    # 強制過期
    with sf() as session:
        record = (
            session.query(EmployeeOffboardingRecord).filter_by(employee_id=emp.id).one()
        )
        record.magic_link_expires_at = datetime.now() - timedelta(days=1)
        session.commit()

    r = client.get(f"/api/offboarding/download?token={token}")
    assert r.status_code == 410
    assert r.json()["detail"] == "LINK_NO_LONGER_VALID"


def test_download_returns_410_after_max_downloads(
    integrated_client,
    admin_login,
    employee_factory,
    leave_quota_factory,
):
    """連下 3 次成功，第 4 次 → 410 Gone（MAX_DOWNLOADS 上限）。"""
    client, _ = integrated_client
    emp, token = _setup_magic_link(
        client, admin_login, employee_factory, leave_quota_factory
    )

    # 前 3 次應全部成功
    for i in range(3):
        with patch(
            "api.offboarding.build_offboarding_zip",
            return_value=_FAKE_ZIP,
        ):
            r = client.get(f"/api/offboarding/download?token={token}")
        assert r.status_code == 200, f"第 {i + 1} 次下載應成功，實際：{r.status_code}"

    # 第 4 次 → 410
    r = client.get(f"/api/offboarding/download?token={token}")
    assert r.status_code == 410
    assert r.json()["detail"] == "LINK_NO_LONGER_VALID"
