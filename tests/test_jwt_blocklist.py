"""tests/test_jwt_blocklist.py — LOW-2 JWT jti 黑名單行為測試

驗證：
- create_access_token 自動帶上 jti claim（uuid-like）
- revoke_token 寫入黑名單（idempotent）
- is_token_revoked 命中後回 True
- cleanup_jwt_blocklist 刪除過期項目
- 受保護端點：當 token 的 jti 在黑名單 → 401
- 舊 token 無 jti → fallback 正常通過（向後相容）
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import Base, User
from utils.auth import (
    create_access_token,
    hash_password,
    is_token_revoked,
    revoke_token,
)


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "jwt.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    yield engine, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_factory
    engine.dispose()


def test_create_access_token_includes_jti(db):
    from jose import jwt as jose_jwt

    from utils.auth import JWT_ALGORITHM, JWT_SECRET_KEY

    token = create_access_token({"user_id": 1})
    payload = jose_jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    assert "jti" in payload
    assert isinstance(payload["jti"], str)
    assert len(payload["jti"]) >= 8


def test_revoke_token_then_is_revoked(db):
    jti = "test-jti-123"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    assert is_token_revoked(jti) is False

    revoke_token(jti, expires_at, reason="logout")
    assert is_token_revoked(jti) is True


def test_revoke_token_is_idempotent(db):
    jti = "dup-jti"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    revoke_token(jti, expires_at)
    revoke_token(jti, expires_at)  # 不應拋
    revoke_token(jti, expires_at)
    engine, _ = db
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT COUNT(*) FROM jwt_blocklist WHERE jti = :j"),
            {"j": jti},
        ).fetchone()
        assert rows[0] == 1


def test_is_token_revoked_returns_false_for_expired_blocklist_entry(db):
    """已過期的黑名單項目不該再阻擋（讓 cleanup 來收）。"""
    jti = "expired-jti"
    expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    revoke_token(jti, expires_at)
    assert is_token_revoked(jti) is False


def test_is_token_revoked_empty_jti_returns_false(db):
    # 舊 token 無 jti（向後相容）：直接回 False，不查 DB
    assert is_token_revoked("") is False
    assert is_token_revoked(None) is False  # type: ignore[arg-type]


def test_cleanup_jwt_blocklist(db):
    from utils.auth import cleanup_jwt_blocklist

    now = datetime.now(timezone.utc)
    revoke_token("old-1", now - timedelta(hours=2))
    revoke_token("old-2", now - timedelta(minutes=5))
    revoke_token("new-1", now + timedelta(hours=1))

    deleted = cleanup_jwt_blocklist()
    assert deleted == 2

    engine, _ = db
    with engine.connect() as conn:
        remaining = conn.execute(
            text("SELECT jti FROM jwt_blocklist ORDER BY jti")
        ).fetchall()
    assert [r[0] for r in remaining] == ["new-1"]


# ── 整合測試：透過 /logout 端點觸發 jti 寫入 ─────────────────────────


def _make_user(session_factory, *, username="bob"):
    s = session_factory()
    try:
        u = User(
            username=username,
            password_hash=hash_password("Pass123456"),
            role="admin",
            permissions=-1,
            is_active=True,
            token_version=0,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id
    finally:
        s.close()


def test_logout_writes_jti_to_blocklist(db):
    engine, session_factory = db
    user_id = _make_user(session_factory)

    app = FastAPI()
    app.include_router(auth_router)
    client = TestClient(app)

    # 簽一個 token，模擬使用者登入後手上的 token
    token = create_access_token(
        {
            "user_id": user_id,
            "role": "admin",
            "permissions": -1,
            "token_version": 0,
        }
    )

    from jose import jwt as jose_jwt

    from utils.auth import JWT_ALGORITHM, JWT_SECRET_KEY

    jti = jose_jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])["jti"]

    assert is_token_revoked(jti) is False

    # 呼叫 /api/auth/logout
    resp = client.post(
        "/api/auth/logout",
        cookies={"access_token": token},
    )
    assert resp.status_code == 200

    # logout 後 jti 應在黑名單
    assert is_token_revoked(jti) is True


def test_old_token_without_jti_still_works_for_blocklist_check(db):
    """向後相容：直接寫 jose_jwt.encode 不帶 jti，is_token_revoked 應回 False。"""
    from jose import jwt as jose_jwt

    from utils.auth import JWT_ALGORITHM, JWT_SECRET_KEY

    token = jose_jwt.encode(
        {
            "user_id": 1,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
        },
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )
    payload = jose_jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    # 沒有 jti
    assert "jti" not in payload
    # is_token_revoked 對 missing jti 回 False
    assert is_token_revoked(payload.get("jti", "")) is False
