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


@pytest.fixture
def client_with_session(tmp_path):
    """同 client fixture 但額外 yield session_factory，供需要建 User / token 的測試使用。"""
    db_path = tmp_path / "refresh-imp-session-test.sqlite"
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
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_refresh_rejects_impersonation_token_via_staff_rotation_path(
    client_with_session,
):
    """staff_refresh_token cookie 存在時走 rotation 路徑，但帶 impersonation claim 的
    access_token 應在進入 rotation 前被頂層拒絕 → 401（P2 fix 驗證）。

    FIX 前（bug）：rotation 路徑先 return 200 乾淨 token，impersonation 歸屬被洗掉。
    FIX 後：頂層檢查先攔截，回 401 「模擬工作階段不可刷新」。

    以真實 User + issue_refresh_token 建立有效 staff_refresh_token，
    確保在 fix 前 rotation 路徑確實能成功（保證測試具鑑別力）。
    """
    from models.auth import User
    from services.staff_refresh import issue_refresh_token
    from utils.auth import hash_password

    client, session_factory = client_with_session

    # 建立真實 User
    with session_factory() as session:
        user = User(
            username="refresh_imp_tester",
            password_hash=hash_password("Pass1234!"),
            role="hr",
            is_active=True,
            token_version=0,
            permission_names=["*"],
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        user_id = user.id

    # 為該 User 簽發真實 staff_refresh_token（寫 DB）
    raw_refresh, _ = issue_refresh_token(user_id, user_agent="test", ip="127.0.0.1")

    # 建立帶 impersonation claim 的 access_token
    imp_token = create_access_token(
        {
            "user_id": user_id,
            "role": "hr",
            "name": "HR 甲",
            "token_version": 0,
            "impersonated_by": 999,
            "impersonated_by_name": "王管理員",
            "impersonation_mode": "write",
        }
    )

    # 同時傳 staff_refresh_token（有效）+ access_token（帶 impersonation）
    resp = client.post(
        "/api/auth/refresh",
        cookies={
            "access_token": imp_token,
            "staff_refresh_token": raw_refresh,
        },
    )
    # Fix 前：rotation path return 200（bug）；Fix 後：頂層攔截 → 401
    assert (
        resp.status_code == 401
    ), f"帶 impersonation token 應回 401，got {resp.status_code}: {resp.text}"
    assert "模擬" in resp.json().get(
        "detail", ""
    ), f"detail 應含「模擬」，got: {resp.json()}"
