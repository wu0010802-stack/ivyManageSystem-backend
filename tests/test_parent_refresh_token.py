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
