"""refresh 端點拒絕帶 impersonation claim 的 token。

防止模擬 session 被 refresh 升級成乾淨 token（silent escalation）。
"""

import models.base as base_module
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.auth import _account_failures, _ip_attempts, router as auth_router
from models.database import Base
from utils.auth import create_access_token


@pytest.fixture
def client(tmp_path):
    """建立 in-memory SQLite + TestClient，並注入 auth router。"""
    db_path = tmp_path / "refresh-impersonation-test.sqlite"
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

    with TestClient(app) as c:
        yield c

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_refresh_rejects_impersonation_token(client):
    token = create_access_token(
        {
            "user_id": 5,
            "employee_id": 5,
            "role": "teacher",
            "name": "老師A",
            "token_version": 0,
            "impersonated_by": 1,
            "impersonated_by_name": "王小明",
            "impersonation_mode": "readonly",
        }
    )
    resp = client.post("/api/auth/refresh", cookies={"access_token": token})
    assert resp.status_code == 401
    # 確認沒有發回新的乾淨 access_token（不會被升級）
