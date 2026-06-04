"""api/permissions_admin.py 整合測試（DB-driven 自訂角色 CRUD）。"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, User
from models.permission_models import PermissionDefinition, Role
from utils.auth import hash_password


def _seed_core(session):
    """seed 1 個 admin user + 1 個無 ROLES_MANAGE 的 user + 3 個 is_core permission + 2 個 is_core role。"""
    session.add(
        PermissionDefinition(
            code="EMPLOYEES_READ", label="員工檢視", group_name="員工", is_core=True
        )
    )
    session.add(
        PermissionDefinition(
            code="ROLES_MANAGE", label="角色與權限管理", group_name="系統", is_core=True
        )
    )
    session.add(
        PermissionDefinition(
            code="DASHBOARD", label="儀表板", group_name="基礎", is_core=True
        )
    )
    session.add(
        Role(
            code="admin",
            label="系統管理員",
            description="全部",
            permissions=["*"],
            is_core=True,
        )
    )
    session.add(
        Role(
            code="teacher",
            label="教師",
            description="基礎",
            permissions=["DASHBOARD"],
            is_core=True,
        )
    )
    session.add(
        User(
            username="admin_u",
            password_hash=hash_password("p"),
            role="admin",
            permission_names=["*"],
        )
    )
    session.add(
        User(
            username="teacher_u",
            password_hash=hash_password("p"),
            role="teacher",
            permission_names=["DASHBOARD"],
        )
    )
    session.commit()


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "perm-admin.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    from api.permissions_admin import router as perm_admin_router
    from api.auth import router as auth_router

    app = FastAPI()
    app.include_router(perm_admin_router)
    app.include_router(auth_router)

    session = session_factory()
    _seed_core(session)
    session.close()

    with TestClient(app) as c:
        yield c, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _admin_login(client):
    resp = client.post("/api/auth/login", json={"username": "admin_u", "password": "p"})
    assert resp.status_code == 200
    return resp


def _teacher_login(client):
    resp = client.post(
        "/api/auth/login", json={"username": "teacher_u", "password": "p"}
    )
    assert resp.status_code == 200
    return resp


# ====================================================================
# PermissionDefinition CRUD — 已移除（只保留角色管理）
# ====================================================================


class TestPermissionDefinitionEndpointsRemoved:
    """權限定義 CRUD 端點已移除，只保留角色管理。鎖住簡化，避免被加回。"""

    def test_create_definition_route_removed(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.post(
            "/api/permissions/definitions", json={"code": "X_PERM", "label": "x"}
        )
        assert resp.status_code == 404

    def test_update_definition_route_removed(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.put("/api/permissions/definitions/EMPLOYEES_READ", json={"label": "x"})
        assert resp.status_code == 404

    def test_delete_definition_route_removed(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.delete("/api/permissions/definitions/EMPLOYEES_READ")
        assert resp.status_code == 404


# ====================================================================
# Role CRUD
# ====================================================================


class TestRoleCRUD:
    def test_create_role_success(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.post(
            "/api/roles",
            json={
                "code": "custom_principal",
                "label": "兼會計園長",
                "description": "principal + SALARY_WRITE",
                "permissions": ["DASHBOARD", "EMPLOYEES_READ"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["code"] == "custom_principal"
        assert resp.json()["is_core"] is False

    def test_create_role_with_unknown_permission_returns_422(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.post(
            "/api/roles",
            json={
                "code": "bad_role",
                "label": "x",
                "permissions": ["UNKNOWN_PERM_XYZ"],
            },
        )
        assert resp.status_code == 422
        assert "UNKNOWN_PERM_XYZ" in resp.json()["detail"]

    def test_create_role_with_wildcard_allowed(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.post(
            "/api/roles", json={"code": "super", "label": "s", "permissions": ["*"]}
        )
        assert resp.status_code == 200

    def test_create_duplicate_code_returns_422(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.post("/api/roles", json={"code": "admin", "label": "重複"})
        assert resp.status_code == 422

    def test_create_invalid_code_pattern_returns_422(self, client):
        c, _ = client
        _admin_login(c)
        for bad in ["UPPERCASE", "with-dash", "123lead", ""]:
            resp = c.post("/api/roles", json={"code": bad, "label": "x"})
            assert resp.status_code == 422, f"bad role code {bad!r} should 422"

    def test_update_is_core_permissions_returns_409(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.put(
            "/api/roles/teacher", json={"permissions": ["DASHBOARD", "EMPLOYEES_READ"]}
        )
        assert resp.status_code == 409
        assert "核心" in resp.json()["detail"]

    def test_update_is_core_label_success(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.put("/api/roles/teacher", json={"label": "老師（改）"})
        assert resp.status_code == 200
        assert resp.json()["label"] == "老師（改）"

    def test_update_custom_role_permissions_bumps_user_token_version(self, client):
        c, sf = client
        _admin_login(c)
        c.post(
            "/api/roles",
            json={"code": "tmp_r", "label": "x", "permissions": ["DASHBOARD"]},
        )
        # 建一個 user 用此 role 且 permission_names IS NULL（依角色預設）
        session = sf()
        from models.database import User
        from utils.auth import hash_password

        u = User(
            username="u_tmp",
            password_hash=hash_password("p"),
            role="tmp_r",
            permission_names=None,
        )
        session.add(u)
        session.commit()
        old_token_v = u.token_version or 0
        session.close()
        # PUT permissions
        c.put("/api/roles/tmp_r", json={"permissions": ["DASHBOARD", "EMPLOYEES_READ"]})
        session = sf()
        u = session.query(User).filter_by(username="u_tmp").first()
        assert (u.token_version or 0) > old_token_v
        session.close()

    def test_delete_is_core_returns_409(self, client):
        c, _ = client
        _admin_login(c)
        resp = c.delete("/api/roles/teacher")
        assert resp.status_code == 409

    def test_delete_role_with_user_reference_returns_409(self, client):
        c, sf = client
        _admin_login(c)
        c.post("/api/roles", json={"code": "tmp_used", "label": "x"})
        session = sf()
        from models.database import User
        from utils.auth import hash_password

        u = User(
            username="u_used",
            password_hash=hash_password("p"),
            role="tmp_used",
            permission_names=[],
        )
        session.add(u)
        session.commit()
        session.close()
        resp = c.delete("/api/roles/tmp_used")
        assert resp.status_code == 409
        assert "1 個帳號" in resp.json()["detail"]

    def test_delete_custom_role_success(self, client):
        c, _ = client
        _admin_login(c)
        c.post("/api/roles", json={"code": "tmp_unused", "label": "x"})
        resp = c.delete("/api/roles/tmp_unused")
        assert resp.status_code == 200
