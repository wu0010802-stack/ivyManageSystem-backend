"""Integration test: GET /api/permissions returns extended role response with description field."""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import models.permission_models  # noqa: F401 — 確保 PermissionDefinition/RoleTemplate 進 Base.metadata
from models.database import Base, User
from api.auth import router as auth_router
from utils.auth import hash_password

from tests._seed_helpers import seed_default_permissions_and_roles


@pytest.fixture
def client(tmp_path):
    """最小 app + SQLite + 7 預設 role 已 seed。

    R6-1 後 GET /auth/permissions 須登入（原 public 契約已廢止——匿名可拉整份
    RBAC 模型屬洩漏），fixture 另 seed 一個 admin 並完成登入。
    """
    db_path = tmp_path / "permissions-endpoint.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    with session_factory() as setup_session:
        seed_default_permissions_and_roles(setup_session)
        setup_session.add(
            User(
                username="perm_ep_admin",
                password_hash=hash_password("AdminPass1234"),
                role="admin",
                permission_names=["*"],
                is_active=True,
                must_change_password=False,
            )
        )
        setup_session.commit()

    app = FastAPI()
    app.include_router(auth_router)
    with TestClient(app) as c:
        res = c.post(
            "/api/auth/login",
            json={"username": "perm_ep_admin", "password": "AdminPass1234"},
        )
        assert res.status_code == 200, f"fixture 登入失敗: {res.text}"
        yield c

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_get_permissions_endpoint_returns_role_descriptions(client):
    """GET /api/auth/permissions 應回傳 7 個 role 且每個含 label/description/permissions。"""
    response = client.get("/api/auth/permissions")
    assert response.status_code == 200
    payload = response.json()
    assert "roles" in payload
    roles = payload["roles"]
    expected_roles = {
        "admin",
        "principal",
        "supervisor",
        "hr",
        "accountant",
        "teacher",
        "parent",
    }
    assert expected_roles == set(
        roles.keys()
    ), f"角色不齊: 缺 {expected_roles - set(roles)}, 多 {set(roles) - expected_roles}"
    for role_key, role_data in roles.items():
        assert "label" in role_data
        assert "description" in role_data and len(role_data["description"]) > 0
        assert "permissions" in role_data


def test_get_permissions_endpoint_admin_uses_wildcard(client):
    """admin 的 permissions 是 ['*']。"""
    response = client.get("/api/auth/permissions")
    payload = response.json()
    assert payload["roles"]["admin"]["permissions"] == ["*"]
