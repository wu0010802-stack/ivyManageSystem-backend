"""Integration test: GET /api/permissions returns extended role response with description field."""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.auth import router as auth_router


@pytest.fixture
def client():
    """無 auth 的最小 app；GET /permissions 是 public。"""
    app = FastAPI()
    app.include_router(auth_router)
    with TestClient(app) as c:
        yield c


def test_get_permissions_endpoint_returns_role_descriptions(client):
    """GET /api/auth/permissions 應回傳 7 個 role 且每個含 label/description/permissions。"""
    response = client.get("/api/auth/permissions")
    assert response.status_code == 200
    payload = response.json()
    assert "roles" in payload
    roles = payload["roles"]
    expected_roles = {"admin", "principal", "supervisor", "hr", "accountant", "teacher", "parent"}
    assert expected_roles == set(roles.keys()), (
        f"角色不齊: 缺 {expected_roles - set(roles)}, 多 {set(roles) - expected_roles}"
    )
    for role_key, role_data in roles.items():
        assert "label" in role_data
        assert "description" in role_data and len(role_data["description"]) > 0
        assert "permissions" in role_data


def test_get_permissions_endpoint_admin_uses_wildcard(client):
    """admin 的 permissions 是 ['*']。"""
    response = client.get("/api/auth/permissions")
    payload = response.json()
    assert payload["roles"]["admin"]["permissions"] == ["*"]
