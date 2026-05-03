"""家長 refresh token 端點測試。

涵蓋：
- happy path: rotation 後舊 token 失效、新 token 可 refresh
- reuse detection: 重用 used token 觸發 family revoke
- race window: 5 秒內 race 不誤判
- expired / revoked / missing 情境
- multi-device family 隔離
- logout 只踢當前 family
"""

import hashlib
import os
import sys
import time
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.parent_portal import (
    parent_router as parent_portal_router,
    init_parent_line_service,
)
from api.parent_portal.auth import _bind_failures
from models.database import (
    Base,
    ParentRefreshToken,
    User,
)


class FakeLineLoginService:
    def __init__(self, sub_map):
        self.sub_map = sub_map

    def is_configured(self):
        return True

    def verify_id_token(self, id_token):
        if id_token in self.sub_map:
            return {"sub": self.sub_map[id_token], "aud": "test", "name": "x"}
        raise HTTPException(status_code=401, detail="invalid")


@pytest.fixture
def parent_client(tmp_path):
    db_path = tmp_path / "refresh.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    SessionLocal = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = SessionLocal

    Base.metadata.create_all(engine)
    _bind_failures.clear()

    init_parent_line_service(
        FakeLineLoginService({"token-AAAAAA": "U_A", "token-BBBBBB": "U_B"})
    )

    app = FastAPI()
    app.include_router(parent_portal_router)

    with TestClient(app) as client:
        yield client, SessionLocal

    base_module._engine = old_engine
    base_module._SessionFactory = old_factory
    engine.dispose()


def _make_parent(session, line_user_id):
    user = User(
        employee_id=None,
        username=f"parent_line_{line_user_id}",
        password_hash="!LINE_ONLY",
        role="parent",
        permissions=0,
        is_active=True,
        must_change_password=False,
        line_user_id=line_user_id,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, session_factory, id_token, line_user_id):
    """走 liff-login 拿到 (access_cookie, refresh_cookie)。"""
    with session_factory() as s:
        _make_parent(s, line_user_id)
        s.commit()
    resp = client.post("/api/parent/auth/liff-login", json={"id_token": id_token})
    assert resp.status_code == 200
    return resp.cookies["access_token"], resp.cookies["parent_refresh_token"]


# ── Happy path: rotation ────────────────────────────────────────────────


def test_refresh_rotates_token_and_old_token_fails(parent_client):
    client, session_factory = parent_client
    _, old_refresh = _login(client, session_factory, "token-AAAAAA", "U_A")

    resp = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert resp.status_code == 200
    new_refresh = resp.cookies.get("parent_refresh_token")
    assert new_refresh is not None
    assert new_refresh != old_refresh

    # DB 有兩筆，舊筆 used_at 填上、parent_token_id 串上
    with session_factory() as s:
        rows = s.query(ParentRefreshToken).order_by(ParentRefreshToken.id).all()
        assert len(rows) == 2
        assert rows[0].used_at is not None
        assert rows[1].used_at is None
        assert rows[1].parent_token_id == rows[0].id
        assert rows[1].family_id == rows[0].family_id

    # 用舊 refresh 再次 refresh → 在 race window 外應 401（reuse）
    # 為避開 5 秒寬容窗，sleep 6 秒
    time.sleep(6)
    resp2 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert resp2.status_code == 401


def test_refresh_reuse_outside_race_window_revokes_family(parent_client):
    """超過 5 秒後拿 used token → 整 family revoke、token_version bump。"""
    client, session_factory = parent_client
    _, old_refresh = _login(client, session_factory, "token-AAAAAA", "U_A")

    # 第一次 rotation 成功，得到 new1
    r1 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert r1.status_code == 200
    new1 = r1.cookies["parent_refresh_token"]

    # 等 6 秒（避開 race window），用 old_refresh 再 refresh → reuse
    time.sleep(6)
    r2 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert r2.status_code == 401

    # family 全 revoked、token_version 升
    with session_factory() as s:
        rows = s.query(ParentRefreshToken).all()
        assert all(r.revoked_at is not None for r in rows)
        user = s.query(User).filter(User.line_user_id == "U_A").first()
        assert user.token_version == 1

    # 連 new1 也用不了
    r3 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": new1},
    )
    assert r3.status_code == 401


def test_refresh_race_within_window_returns_409(parent_client):
    """5 秒內 race：第二個請求收 409，不誤觸 reuse。"""
    client, session_factory = parent_client
    _, old_refresh = _login(client, session_factory, "token-AAAAAA", "U_A")

    r1 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert r1.status_code == 200

    # 立刻（< 5s）再用 old_refresh
    r2 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert r2.status_code == 409

    # family 不應被 revoke
    with session_factory() as s:
        revoked = (
            s.query(ParentRefreshToken)
            .filter(ParentRefreshToken.revoked_at.isnot(None))
            .count()
        )
        assert revoked == 0


def test_refresh_missing_cookie_returns_401(parent_client):
    client, _ = parent_client
    r = client.post("/api/parent/auth/refresh")
    assert r.status_code == 401


def test_refresh_unknown_token_returns_401(parent_client):
    client, _ = parent_client
    r = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": "never-issued-by-server"},
    )
    assert r.status_code == 401


def test_refresh_expired_token_returns_401(parent_client):
    client, session_factory = parent_client
    _, old_refresh = _login(client, session_factory, "token-AAAAAA", "U_A")
    # 直接 SQL 把 expires_at 推到過去
    with session_factory() as s:
        row = s.query(ParentRefreshToken).first()
        row.expires_at = datetime.now() - timedelta(hours=1)
        s.commit()
    r = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert r.status_code == 401


def test_refresh_revoked_token_returns_401(parent_client):
    client, session_factory = parent_client
    _, old_refresh = _login(client, session_factory, "token-AAAAAA", "U_A")
    with session_factory() as s:
        row = s.query(ParentRefreshToken).first()
        row.revoked_at = datetime.now()
        s.commit()
    r = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert r.status_code == 401


def test_refresh_disabled_user_returns_401(parent_client):
    client, session_factory = parent_client
    _, old_refresh = _login(client, session_factory, "token-AAAAAA", "U_A")
    with session_factory() as s:
        u = s.query(User).filter(User.line_user_id == "U_A").first()
        u.is_active = False
        s.commit()
    r = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": old_refresh},
    )
    assert r.status_code == 401


def test_multi_device_family_isolation(parent_client):
    """裝置 A reuse 觸發後，裝置 B 的 family 不受影響。"""
    client, session_factory = parent_client

    # 裝置 A 登入（會發 family A）
    _, refresh_A = _login(client, session_factory, "token-AAAAAA", "U_A")

    # 同 user 模擬第二裝置：直接呼叫 issue_refresh_token 寫一筆新 family
    from api.parent_portal.auth import (
        _issue_refresh_token,
        _gen_refresh_raw,
        _hash_refresh,
    )
    import uuid

    with session_factory() as s:
        u = s.query(User).filter(User.line_user_id == "U_A").first()
        raw_B = _gen_refresh_raw()
        row_B = ParentRefreshToken(
            user_id=u.id,
            family_id=str(uuid.uuid4()),
            token_hash=_hash_refresh(raw_B),
            expires_at=datetime.now() + timedelta(days=30),
        )
        s.add(row_B)
        s.commit()

    # 把 family A 的 refresh 用一次（rotation），然後等 6s 再 reuse
    r1 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": refresh_A},
    )
    assert r1.status_code == 200
    time.sleep(6)
    r2 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": refresh_A},
    )
    assert r2.status_code == 401  # reuse 觸發

    # ⚠ family A 被 revoke + user.token_version+=1，但 family B 也仍在 DB
    # 不過實際上 token_version bump 後，access token 全廢；refresh token 本身
    # 不檢查 token_version。所以 family B 的 refresh 仍可用：
    r3 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": raw_B},
    )
    assert r3.status_code == 200


def test_logout_revokes_current_family_only(parent_client):
    """logout 應 revoke 當前 family + 清 cookie；其他裝置 family 不受影響。"""
    client, session_factory = parent_client
    access_A, refresh_A = _login(client, session_factory, "token-AAAAAA", "U_A")

    # 第二裝置（family B）
    from api.parent_portal.auth import _gen_refresh_raw, _hash_refresh
    import uuid

    with session_factory() as s:
        u = s.query(User).filter(User.line_user_id == "U_A").first()
        raw_B = _gen_refresh_raw()
        row_B = ParentRefreshToken(
            user_id=u.id,
            family_id=str(uuid.uuid4()),
            token_hash=_hash_refresh(raw_B),
            expires_at=datetime.now() + timedelta(days=30),
        )
        s.add(row_B)
        s.commit()
        family_B = row_B.family_id

    # 登出（帶 access_A + refresh_A）
    r = client.post(
        "/api/parent/auth/logout",
        cookies={"access_token": access_A, "parent_refresh_token": refresh_A},
    )
    assert r.status_code == 204

    # family A 全 revoked、family B 不變
    with session_factory() as s:
        rows = s.query(ParentRefreshToken).all()
        for row in rows:
            if row.family_id == family_B:
                assert row.revoked_at is None
            else:
                assert row.revoked_at is not None

    # cookie 已清（response 寫了清除指令；測試端 cookies jar 可能仍保留舊值
    # 因此用 family B 的 raw 跨裝置去 refresh 應仍 OK）
    r2 = client.post(
        "/api/parent/auth/refresh",
        cookies={"parent_refresh_token": raw_B},
    )
    assert r2.status_code == 200
