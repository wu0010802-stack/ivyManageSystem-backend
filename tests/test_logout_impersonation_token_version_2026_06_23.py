"""模擬（impersonation）中 /logout 應作廢真正的操作主體（admin），不可 bump 無辜 target。

qa-loop #14（2026-06-23，與 #4 同根）：模擬中 access_token cookie 持有的是模擬 token
（user_id=target、impersonated_by=admin）。logout 讀 payload.user_id 拿到 target，遂遞增
target.token_version——這會誤踢真實 target 使用者自己的合法 session，且 admin 本人的
token_version 未被觸及（admin 其他真實 session 不因這次「登出」作廢）。修法：模擬中
logout 以 impersonated_by(admin) 為作廢主體。模擬 token 本身仍經 jti 黑名單失效。
"""

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
from models.database import Base, User
from utils.auth import create_access_token, hash_password


@pytest.fixture
def auth_client(tmp_path):
    db_path = tmp_path / "logout_imp_tv.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed_user(session, username, role, token_version=0):
    u = User(
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role=role,
        permission_names=["*"] if role == "admin" else [],
        is_active=True,
        must_change_password=False,
        token_version=token_version,
    )
    session.add(u)
    session.flush()
    return u.id


def _impersonation_token(target_id, admin_id, target_tv=0):
    return create_access_token(
        {
            "user_id": target_id,
            "employee_id": None,
            "role": "teacher",
            "name": "目標老師",
            "permission_names": [],
            "token_version": target_tv,
            "impersonated_by": admin_id,
            "impersonated_by_name": "系統管理員",
            "impersonation_mode": "write",
        }
    )


def _normal_token(user_id, token_version=0):
    return create_access_token(
        {
            "user_id": user_id,
            "employee_id": None,
            "role": "admin",
            "name": "系統管理員",
            "permission_names": ["*"],
            "token_version": token_version,
        }
    )


def test_logout_during_impersonation_bumps_admin_not_target(auth_client):
    client, sf = auth_client
    with sf() as s:
        admin_id = _seed_user(s, "admin", "admin", token_version=0)
        target_id = _seed_user(s, "tgt", "teacher", token_version=0)
        s.commit()
    imp_token = _impersonation_token(target_id, admin_id)

    res = client.post("/api/auth/logout", cookies={"access_token": imp_token})
    assert res.status_code == 200, res.text

    with sf() as s:
        admin = s.query(User).filter(User.id == admin_id).first()
        target = s.query(User).filter(User.id == target_id).first()
        assert admin.token_version == 1, "模擬中登出應作廢 admin（真正操作主體）"
        assert (
            target.token_version == 0
        ), "不可 bump 無辜 target 的 token_version（會誤踢其真實 session）"


def test_logout_normal_bumps_self(auth_client):
    """非模擬登出維持原行為：bump 自己的 token_version。"""
    client, sf = auth_client
    with sf() as s:
        uid = _seed_user(s, "u", "admin", token_version=0)
        s.commit()
    token = _normal_token(uid, 0)

    res = client.post("/api/auth/logout", cookies={"access_token": token})
    assert res.status_code == 200, res.text

    with sf() as s:
        u = s.query(User).filter(User.id == uid).first()
        assert u.token_version == 1
