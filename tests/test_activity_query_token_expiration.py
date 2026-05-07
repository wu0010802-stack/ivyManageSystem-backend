"""驗證公開查詢碼到期機制：register 寫 issued_at、查詢端點驗 TTL、reject 清 issued_at。

威脅：Phase 3 查詢碼一旦發出永久有效。家長截圖、LINE 轉傳、換手機後二手釋出，
任何人配上 phone 都能查全家報名。

修法：query_token_issued_at + ACTIVITY_QUERY_TOKEN_TTL_DAYS（預設 180）。
過期或舊 reg（issued_at=NULL）一律 404，與 token 不存在 / phone 錯回同一訊息
（避免洩漏「過期」與其他失敗的差別）。

Refs: 資安掃描 2026-05-07 P0。
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity._shared import (
    _hash_query_token,
    is_query_token_expired,
    _query_token_ttl_days,
)
from api.activity.public import (
    _public_query_limiter_instance,
    _public_register_limiter_instance,
)
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
)


@pytest.fixture
def expiration_client(tmp_path):
    db_path = tmp_path / "expiration.sqlite"
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
    _public_register_limiter_instance._timestamps.clear()
    _public_query_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    _public_register_limiter_instance._timestamps.clear()
    _public_query_limiter_instance._timestamps.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _seed_reg(session, *, issued_at, parent_phone="0900-111-222"):
    sy, sem = _term()
    classroom = Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
    session.add(classroom)
    session.flush()
    plain_token = "test_token_abc_xyz_1234"
    reg = ActivityRegistration(
        student_name="王小明",
        birthday=date(2020, 5, 1),
        class_name="海豚班",
        school_year=sy,
        semester=sem,
        parent_phone=parent_phone,
        is_active=True,
        match_status="matched",
        query_token_hash=_hash_query_token(plain_token),
        query_token_issued_at=issued_at,
    )
    session.add(reg)
    session.flush()
    return reg, plain_token


class TestExpirationHelper:
    def test_none_issued_at_is_expired(self):
        assert is_query_token_expired(None) is True

    def test_fresh_token_not_expired(self):
        assert is_query_token_expired(datetime.now()) is False

    def test_token_within_ttl_not_expired(self):
        assert is_query_token_expired(datetime.now() - timedelta(days=179)) is False

    def test_token_past_ttl_is_expired(self):
        assert is_query_token_expired(datetime.now() - timedelta(days=181)) is True

    def test_ttl_env_override(self, monkeypatch):
        monkeypatch.setenv("ACTIVITY_QUERY_TOKEN_TTL_DAYS", "30")
        assert _query_token_ttl_days() == 30
        # 31 天 → 過期
        assert is_query_token_expired(datetime.now() - timedelta(days=31)) is True
        # 29 天 → 未過期
        assert is_query_token_expired(datetime.now() - timedelta(days=29)) is False

    def test_ttl_env_invalid_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("ACTIVITY_QUERY_TOKEN_TTL_DAYS", "not-a-number")
        assert _query_token_ttl_days() == 180
        monkeypatch.setenv("ACTIVITY_QUERY_TOKEN_TTL_DAYS", "0")
        assert _query_token_ttl_days() == 180
        monkeypatch.setenv("ACTIVITY_QUERY_TOKEN_TTL_DAYS", "-5")
        assert _query_token_ttl_days() == 180


class TestQueryByTokenExpiration:
    def test_fresh_token_succeeds(self, expiration_client):
        client, sf = expiration_client
        with sf() as s:
            reg, plain_token = _seed_reg(s, issued_at=datetime.now())
            s.commit()

        res = client.post(
            "/api/activity/public/query-by-token",
            json={"token": plain_token, "parent_phone": "0900-111-222"},
        )
        assert res.status_code == 200, res.text

    def test_expired_token_returns_404_same_message(self, expiration_client):
        """過期 token 必須回 404 + 與 token 不存在同訊息（避免 oracle）"""
        client, sf = expiration_client
        with sf() as s:
            reg, plain_token = _seed_reg(
                s, issued_at=datetime.now() - timedelta(days=181)
            )
            s.commit()

        res = client.post(
            "/api/activity/public/query-by-token",
            json={"token": plain_token, "parent_phone": "0900-111-222"},
        )
        assert res.status_code == 404
        assert "查無對應報名" in res.json()["detail"]

    def test_null_issued_at_old_data_returns_404(self, expiration_client):
        """舊資料 issued_at=NULL（backfill 期）一律視為過期"""
        client, sf = expiration_client
        with sf() as s:
            reg, plain_token = _seed_reg(s, issued_at=None)
            s.commit()

        res = client.post(
            "/api/activity/public/query-by-token",
            json={"token": plain_token, "parent_phone": "0900-111-222"},
        )
        assert res.status_code == 404


class TestRegisterStampsIssuedAt:
    def test_register_writes_issued_at(self, expiration_client):
        """成功 register 必須同時寫 query_token_hash + query_token_issued_at"""
        client, sf = expiration_client
        sy, sem = _term()
        with sf() as s:
            classroom = Classroom(
                name="海豚班", is_active=True, school_year=sy, semester=sem
            )
            s.add(classroom)
            s.add(
                ActivityCourse(
                    name="圍棋",
                    price=1000,
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                )
            )
            s.commit()

        before = datetime.now()
        res = client.post(
            "/api/activity/public/register",
            json={
                "name": "王小明",
                "birthday": "2020-05-01",
                "class": "海豚班",
                "parent_phone": "0900-111-222",
                "courses": [{"name": "圍棋", "price": "1000"}],
                "supplies": [],
            },
        )
        after = datetime.now()
        assert res.status_code == 201, res.text

        with sf() as s:
            reg = s.query(ActivityRegistration).first()
            assert reg.query_token_hash is not None
            assert reg.query_token_issued_at is not None
            # 寫入時間應落在請求區間內（容忍 1 秒誤差）
            assert (
                before - timedelta(seconds=1)
                <= reg.query_token_issued_at
                <= after + timedelta(seconds=1)
            )
