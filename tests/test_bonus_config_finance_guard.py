"""BonusConfig PUT 金流守衛：對齊 PUT /insurance/brackets 的 finance_approve + reason。

威脅：bug sweep 2026-05-16 P1-5。原本 PUT /api/config/bonus 只要求 SETTINGS_WRITE
（HR 行政都有）即可改全員獎金基數，繞過 manual_adjust 500K 上限 + 簽核。

修法：require has_finance_approve + reason ≥10 字 + 寫 audit_changes/summary。
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.config import router as config_router
from models.auth import User
from models.database import Base
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "bonus-guard.sqlite"
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
    app.include_router(config_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login(client, sf, *, with_finance: bool):
    """建立 HR 帳號（SETTINGS_WRITE）+ 視 with_finance 決定是否帶金流簽核位元。"""
    perms = ["SETTINGS_WRITE", "SETTINGS_READ"]
    if with_finance:
        perms.append(Permission.ACTIVITY_PAYMENT_APPROVE.value)
    with sf() as s:
        s.add(
            User(
                username="hr_user",
                password_hash=hash_password("TempPass123"),
                role="admin",
                permission_names=perms,
                is_active=True,
            )
        )
        s.commit()
    res = client.post(
        "/api/auth/login",
        json={"username": "hr_user", "password": "TempPass123"},
    )
    assert res.status_code == 200, res.text


class TestBonusConfigFinanceGuard:
    def test_rejects_without_finance_approve(self, client_with_db):
        """只有 SETTINGS_WRITE（無 ACTIVITY_PAYMENT_APPROVE）→ 403。"""
        client, sf = client_with_db
        _login(client, sf, with_finance=False)
        res = client.put(
            "/api/config/bonus",
            json={
                "head_teacher_ab": 2500,
                "reason": "本變更影響全員獎金基數，需 finance_approve",
            },
        )
        assert res.status_code == 403, res.text
        assert "金流簽核" in res.json()["detail"]

    def test_rejects_when_reason_missing(self, client_with_db):
        """帶 finance_approve 但 reason 缺失 → 400。"""
        client, sf = client_with_db
        _login(client, sf, with_finance=True)
        res = client.put(
            "/api/config/bonus",
            json={"head_teacher_ab": 2500},
        )
        assert res.status_code == 400, res.text
        assert "原因" in res.json()["detail"]

    def test_rejects_when_reason_too_short(self, client_with_db):
        """reason < 10 字 → 400。"""
        client, sf = client_with_db
        _login(client, sf, with_finance=True)
        res = client.put(
            "/api/config/bonus",
            json={"head_teacher_ab": 2500, "reason": "短"},
        )
        assert res.status_code == 400, res.text

    def test_accepts_with_finance_and_reason(self, client_with_db):
        """完整守衛通過 → 200。"""
        client, sf = client_with_db
        _login(client, sf, with_finance=True)
        res = client.put(
            "/api/config/bonus",
            json={
                "head_teacher_ab": 2500,
                "config_year": 2026,
                "reason": "依新版獎金條例調整班導 ab 級基數",
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["version"] >= 1
