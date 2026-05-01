"""tests/test_activity_public_query_token_phase3.py

Phase 3 公開查詢碼（query token）— 2026-05-01。

設計重點：
- /public/register response 帶明文 token（真實成功路徑）
- silent path（honeypot/silent-success）也回 token shape，避免 F-030 oracle 復活
- 新 POST /public/query-by-token endpoint：token+phone 雙因素，與三欄查詢並存
- DB 只存 HMAC hash，明文 token 一次性不再重發
- admin reject pending 時 invalidate token hash，舊 token 失效
"""

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
from api.activity import router as activity_router
from api.activity._shared import _hash_query_token
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
    Student,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def phase3_client(tmp_path):
    db_path = tmp_path / "phase3.sqlite"
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


def _seed(session, *, with_student=True):
    sy, sem = _term()
    classroom = Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
    session.add(classroom)
    session.flush()
    session.add(
        ActivityCourse(
            name="圍棋", price=1000, school_year=sy, semester=sem, is_active=True
        )
    )
    if with_student:
        session.add(
            Student(
                student_id="S001",
                name="王小明",
                birthday=date(2020, 5, 10),
                classroom_id=classroom.id,
                parent_phone="0912345678",
                is_active=True,
            )
        )
    session.add(
        User(
            username="admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            is_active=True,
        )
    )
    session.commit()
    return classroom.id


def _register(client, *, name="王小明", phone="0912345678", class_name="海豚班"):
    return client.post(
        "/api/activity/public/register",
        json={
            "name": name,
            "birthday": "2020-05-10",
            "parent_phone": phone,
            "class": class_name,
            "courses": [{"name": "圍棋", "price": "1000"}],
            "supplies": [],
        },
    )


def _login(client):
    r = client.post(
        "/api/auth/login", json={"username": "admin", "password": "TempPass123"}
    )
    assert r.status_code == 200
    return r


class TestRegisterReturnsToken:
    def test_register_response_contains_query_token(self, phase3_client):
        client, sf = phase3_client
        with sf() as s:
            _seed(s)
        res = _register(client)
        assert res.status_code == 201
        body = res.json()
        assert "query_token" in body
        token = body["query_token"]
        assert isinstance(token, str)
        assert len(token) >= 16

    def test_register_token_is_hashed_in_db(self, phase3_client):
        """DB 只存 hash，明文不存。"""
        client, sf = phase3_client
        with sf() as s:
            _seed(s)
        res = _register(client)
        token = res.json()["query_token"]

        with sf() as s:
            reg = s.query(ActivityRegistration).first()
            # 明文 token 不可直接出現在 DB
            assert reg.query_token_hash != token
            # hash 必須對應明文
            assert reg.query_token_hash == _hash_query_token(token)

    def test_silent_reject_path_also_returns_token(self, phase3_client):
        """honeypot 觸發的 silent-reject 也要回 query_token，否則 F-030 oracle 復活。"""
        client, sf = phase3_client
        with sf() as s:
            _seed(s)
        # honeypot 欄位 hp 帶值 → silent-reject
        res = client.post(
            "/api/activity/public/register",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
                "class": "海豚班",
                "courses": [{"name": "圍棋", "price": "1000"}],
                "supplies": [],
                "hp": "bot-trap",
            },
        )
        assert res.status_code == 201
        body = res.json()
        # silent path 必須有 query_token 欄位（即使是假 token）
        assert (
            "query_token" in body
        ), "silent-reject leaked oracle by missing query_token"


class TestQueryByToken:
    def test_query_by_token_with_correct_phone_succeeds(self, phase3_client):
        client, sf = phase3_client
        with sf() as s:
            _seed(s)
        token = _register(client).json()["query_token"]

        res = client.post(
            "/api/activity/public/query-by-token",
            json={"token": token, "parent_phone": "0912345678"},
        )
        assert res.status_code == 200
        body = res.json()
        # 與 /public/query 同 schema
        for key in ("id", "name", "birthday", "courses", "field_state", "updated_at"):
            assert key in body

    def test_query_by_token_wrong_phone_returns_404(self, phase3_client):
        client, sf = phase3_client
        with sf() as s:
            _seed(s)
        token = _register(client).json()["query_token"]

        res = client.post(
            "/api/activity/public/query-by-token",
            json={"token": token, "parent_phone": "0900000000"},
        )
        assert res.status_code == 404

    def test_query_by_invalid_token_returns_404(self, phase3_client):
        client, sf = phase3_client
        with sf() as s:
            _seed(s)
        _register(client)

        res = client.post(
            "/api/activity/public/query-by-token",
            json={
                "token": "this-token-does-not-exist-aaaa",
                "parent_phone": "0912345678",
            },
        )
        assert res.status_code == 404

    def test_short_token_returns_404_not_422(self, phase3_client):
        """token 太短不應走 422（pydantic length 拒絕），否則 status code 變成
        「token 格式合不合法」的 oracle。應與其他不存在的 token 一樣回 404。"""
        client, sf = phase3_client
        with sf() as s:
            _seed(s)
        _register(client)

        res = client.post(
            "/api/activity/public/query-by-token",
            json={"token": "x", "parent_phone": "0912345678"},
        )
        assert (
            res.status_code == 404
        ), f"短 token 應回 404 不應 422（避免格式 vs 不存在 oracle）；實得 {res.status_code}"

    def test_silent_token_does_not_grant_access(self, phase3_client):
        """silent-reject 回的假 token 不應寫進 DB，拿來查必失敗（避免 oracle）。"""
        client, sf = phase3_client
        with sf() as s:
            _seed(s)
        res = client.post(
            "/api/activity/public/register",
            json={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
                "class": "海豚班",
                "courses": [{"name": "圍棋", "price": "1000"}],
                "supplies": [],
                "hp": "bot-trap",
            },
        )
        fake_token = res.json()["query_token"]

        q = client.post(
            "/api/activity/public/query-by-token",
            json={"token": fake_token, "parent_phone": "0912345678"},
        )
        assert q.status_code == 404

    def test_query_by_token_response_does_not_leak_match_internals(self, phase3_client):
        client, sf = phase3_client
        with sf() as s:
            _seed(s)
        token = _register(client).json()["query_token"]

        res = client.post(
            "/api/activity/public/query-by-token",
            json={"token": token, "parent_phone": "0912345678"},
        )
        body = res.json()
        for forbidden in (
            "match_status",
            "pending_review",
            "student_id",
            "classroom_id",
        ):
            assert forbidden not in body


class TestBackwardCompatibility:
    def test_legacy_three_field_query_still_works(self, phase3_client):
        """既有報名沒有 token 的也要能用三欄查詢 — 向後相容契約。"""
        client, sf = phase3_client
        with sf() as s:
            _seed(s)
        _register(client)

        res = client.get(
            "/api/activity/public/query",
            params={
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0912345678",
            },
        )
        assert res.status_code == 200
        body = res.json()
        # query 端點不應外洩 query_token（明文只在 register 那一次）
        assert "query_token" not in body

    def test_legacy_registration_without_token_can_still_use_three_field_query(
        self, phase3_client
    ):
        """直接寫一筆沒有 query_token_hash 的歷史報名，仍能用三欄查到。"""
        client, sf = phase3_client
        with sf() as s:
            _seed(s)
            sy, sem = _term()
            s.add(
                ActivityRegistration(
                    student_name="老資料",
                    birthday="2019-01-01",
                    class_name="海豚班",
                    parent_phone="0911111111",
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                    match_status="unmatched",
                    query_token_hash=None,
                )
            )
            s.commit()

        res = client.get(
            "/api/activity/public/query",
            params={
                "name": "老資料",
                "birthday": "2019-01-01",
                "parent_phone": "0911111111",
            },
        )
        assert res.status_code == 200


class TestRejectInvalidatesToken:
    def test_reject_pending_invalidates_old_token(self, phase3_client):
        client, sf = phase3_client
        with sf() as s:
            _seed(s, with_student=False)  # 無學生 → 進 pending
        token = _register(client).json()["query_token"]

        # 先確認 reject 前能查得到
        ok = client.post(
            "/api/activity/public/query-by-token",
            json={"token": token, "parent_phone": "0912345678"},
        )
        assert ok.status_code == 200
        reg_id = ok.json()["id"]

        # admin reject
        _login(client)
        rej = client.post(
            f"/api/activity/registrations/{reg_id}/reject",
            json={"reason": "測試拒絕"},
        )
        assert rej.status_code == 200

        # 用同一 token 應查不到
        gone = client.post(
            "/api/activity/public/query-by-token",
            json={"token": token, "parent_phone": "0912345678"},
        )
        assert gone.status_code == 404
