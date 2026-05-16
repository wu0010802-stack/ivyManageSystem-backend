"""驗證 appraisal/year_end 簽核與建立操作能正確寫入 created_by / *_signed_by。

威脅來源：bug sweep 2026-05-16 P0-1b。JWT payload 鍵為 `user_id`，但 11 處
誤用 `current_user.get("id")` → 永遠 None → 三層簽核稽核軌跡完全失效。

這支補靜態檢查（test_recorded_by_user_id_fix.py）的不足，從行為層驗證
POST 建立後 DB 欄位確實寫入登入用戶的 user.id。
"""

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
from api.appraisal import appraisal_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.year_end import year_end_router
from models.appraisal import AppraisalCycle
from models.auth import User
from models.database import Base
from models.year_end import YearEndCycle
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "audit-user-id.sqlite"
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
    app.include_router(appraisal_router)
    app.include_router(year_end_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, username, perms, password="TempPass123") -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=int(perms),
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password="TempPass123"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text
    return res


class TestAppraisalCycleCreatedBy:
    def test_create_cycle_records_login_user_id(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            user = _create_user(
                s,
                "auditor",
                Permission.APPRAISAL_READ
                | Permission.APPRAISAL_EVENT_WRITE
                | Permission.APPRAISAL_FINALIZE,
            )
            user_id = user.id
            s.commit()
        _login(client, "auditor")

        res = client.post(
            "/api/appraisal/cycles",
            json={
                "academic_year": 114,
                "semester": "FIRST",
                "start_date": "2025-08-01",
                "end_date": "2026-01-31",
                "base_score_calc_date": "2025-09-15",
                "base_score": 75.5,
            },
        )
        assert res.status_code == 200, res.text
        cycle_id = res.json()["id"]

        with sf() as s:
            cycle = s.query(AppraisalCycle).filter(AppraisalCycle.id == cycle_id).one()
            assert (
                cycle.created_by == user_id
            ), f"created_by 應為登入用戶 {user_id}，但為 {cycle.created_by}（仍是舊 bug）"


class TestYearEndCycleCreatedBy:
    def test_create_year_end_cycle_records_login_user_id(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            user = _create_user(
                s,
                "ye_admin",
                Permission.YEAR_END_READ
                | Permission.YEAR_END_WRITE
                | Permission.YEAR_END_FINALIZE,
            )
            user_id = user.id
            s.commit()
        _login(client, "ye_admin")

        res = client.post(
            "/api/year_end/cycles",
            json={
                "academic_year": 114,
                "start_date": "2025-08-01",
                "end_date": "2026-07-31",
                "bonus_calc_date": "2026-01-15",
            },
        )
        assert res.status_code == 200, res.text
        cycle_id = res.json()["id"]

        with sf() as s:
            cycle = s.query(YearEndCycle).filter(YearEndCycle.id == cycle_id).one()
            assert (
                cycle.created_by == user_id
            ), f"created_by 應為登入用戶 {user_id}，但為 {cycle.created_by}（仍是舊 bug）"
