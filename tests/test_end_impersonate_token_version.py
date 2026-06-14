"""end_impersonate 須校驗 admin_token 的 token_version（C14，bug hunt money-auth 2026-06-14）。

end_impersonate 從 admin_token cookie 重簽新 admin access token，但原本完全不比對
token_version → 密碼變更/重設/撤帳 bump token_version（全域作廢）後，舊 admin_token
cookie 仍可換發有效 admin token，繞過作廢。修法：比對 payload token_version 與 DB
user.token_version（與 refresh 路徑 auth.py:911 對齊），不符 401。
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
    db_path = tmp_path / "end_impersonate_tv.sqlite"
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


def _seed_admin(session, token_version=0):
    u = User(
        username="admin",
        password_hash=hash_password("Passw0rd!"),
        role="admin",
        permission_names=["*"],
        is_active=True,
        must_change_password=False,
        token_version=token_version,
    )
    session.add(u)
    session.flush()
    return u.id


def _admin_token(user_id, token_version):
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


def test_end_impersonate_rejects_stale_token_version(auth_client):
    """admin token_version 已 bump（全域作廢）後，舊 admin_token 不可換發 → 401。"""
    client, sf = auth_client
    with sf() as s:
        admin_id = _seed_admin(s, token_version=0)
        s.commit()
    stale_token = _admin_token(admin_id, token_version=0)

    # 模擬密碼變更/撤帳：bump token_version
    with sf() as s:
        s.query(User).filter(User.id == admin_id).update({"token_version": 1})
        s.commit()

    res = client.post("/api/auth/end-impersonate", cookies={"admin_token": stale_token})
    assert res.status_code == 401, res.text


def test_end_impersonate_accepts_matching_token_version(auth_client):
    """token_version 相符時正常還原管理員身分（200），不誤擋合法結束冒充。"""
    client, sf = auth_client
    with sf() as s:
        admin_id = _seed_admin(s, token_version=3)
        s.commit()
    token = _admin_token(admin_id, token_version=3)

    res = client.post("/api/auth/end-impersonate", cookies={"admin_token": token})
    assert res.status_code == 200, res.text
