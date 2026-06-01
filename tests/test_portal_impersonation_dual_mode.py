import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from utils.permissions import (
    Permission,
    ROLE_TEMPLATES,
    PERMISSION_LABELS,
    has_permission,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_new_portal_permissions_exist():
    assert Permission.PORTAL_PREVIEW.value == "PORTAL_PREVIEW"
    assert Permission.PORTAL_IMPERSONATE.value == "PORTAL_IMPERSONATE"
    assert "PORTAL_PREVIEW" in PERMISSION_LABELS
    assert "PORTAL_IMPERSONATE" in PERMISSION_LABELS


def test_principal_has_preview_not_impersonate():
    principal_perms = ROLE_TEMPLATES["principal"]
    assert Permission.PORTAL_PREVIEW.value in principal_perms
    assert Permission.PORTAL_IMPERSONATE.value not in principal_perms


def test_admin_wildcard_passes_both():
    admin_perms = ROLE_TEMPLATES["admin"]  # ["*"]
    assert has_permission(admin_perms, Permission.PORTAL_PREVIEW)
    assert has_permission(admin_perms, Permission.PORTAL_IMPERSONATE)


# ─── 端對端 impersonate 端點測試 ──────────────────────────────────────────────


import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from models.database import Base, Employee, User
from utils.auth import hash_password, decode_token


@pytest.fixture
def app_and_client(tmp_path):
    """建立 in-memory SQLite + TestClient，並注入 auth router。"""
    db_path = tmp_path / "impersonate-test.sqlite"
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

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _make_employee(session, eid: str, name: str) -> Employee:
    emp = Employee(employee_id=eid, name=name, base_salary=36000, is_active=True)
    session.add(emp)
    session.flush()
    return emp


def _make_user(
    session,
    *,
    employee_id=None,
    username: str,
    password: str = "Pass1234!",
    role: str = "teacher",
    permission_names=None,
) -> User:
    if permission_names is None:
        permission_names = ROLE_TEMPLATES.get(role, [])
    u = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permission_names=permission_names,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client: TestClient, username: str, password: str = "Pass1234!"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


# ─── 端點測試 ─────────────────────────────────────────────────────────────────


class TestImpersonateEndpointMode:
    """mode 參數、權限分流、token claim、防巢狀。"""

    @pytest.fixture
    def setup(self, app_and_client):
        """建立 admin + principal + teacher + teacher2 + other_admin_employee。"""
        client, session_factory = app_and_client

        with session_factory() as session:
            # admin user（無 employee_id，避免自我防衛守衛）
            admin_user = _make_user(
                session,
                username="admin_user",
                role="admin",
                permission_names=["*"],
            )

            # principal user（有自己的員工，用 principal 角色）
            principal_emp = _make_employee(session, "P001", "園長老師")
            principal_user = _make_user(
                session,
                employee_id=principal_emp.id,
                username="principal_user",
                role="principal",
            )

            # target teacher（一般老師，有 employee + user）
            teacher_emp = _make_employee(session, "T001", "老師甲")
            teacher_user = _make_user(
                session,
                employee_id=teacher_emp.id,
                username="teacher_user",
                role="teacher",
            )

            # target teacher2（另一位老師，用於防巢狀測試）
            teacher2_emp = _make_employee(session, "T002", "老師乙")
            teacher2_user = _make_user(
                session,
                employee_id=teacher2_emp.id,
                username="teacher_user2",
                role="teacher",
            )

            # other_admin_employee（員工帳號，role=admin → 禁止冒充）
            other_admin_emp = _make_employee(session, "A002", "副管理員")
            other_admin_user = _make_user(
                session,
                employee_id=other_admin_emp.id,
                username="other_admin",
                role="admin",
                permission_names=["*"],
            )

            session.commit()

            ids = {
                "teacher_emp_id": teacher_emp.id,
                "teacher2_emp_id": teacher2_emp.id,
                "other_admin_emp_id": other_admin_emp.id,
            }

        return client, ids

    def _admin_login(self, client):
        r = _login(client, "admin_user")
        assert r.status_code == 200, f"admin login failed: {r.json()}"
        return r

    def _principal_login(self, client):
        r = _login(client, "principal_user")
        assert r.status_code == 200, f"principal login failed: {r.json()}"
        return r

    def test_admin_readonly_impersonate_sets_mode_claim(self, setup):
        client, ids = setup
        self._admin_login(client)

        resp = client.post(
            "/api/auth/impersonate",
            json={"employee_id": ids["teacher_emp_id"], "mode": "readonly"},
        )
        assert (
            resp.status_code == 200
        ), f"expected 200, got {resp.status_code}: {resp.json()}"

        token = client.cookies.get("access_token")
        assert token, "access_token cookie 未設定"
        payload = decode_token(token)
        assert payload.get("impersonation_mode") == "readonly"
        assert payload.get("impersonated_by") is not None

    def test_admin_write_impersonate_allowed(self, setup):
        client, ids = setup
        self._admin_login(client)

        resp = client.post(
            "/api/auth/impersonate",
            json={"employee_id": ids["teacher_emp_id"], "mode": "write"},
        )
        assert (
            resp.status_code == 200
        ), f"expected 200, got {resp.status_code}: {resp.json()}"

        token = client.cookies.get("access_token")
        payload = decode_token(token)
        assert payload.get("impersonation_mode") == "write"

    def test_principal_cannot_write_impersonate(self, setup):
        client, ids = setup
        self._principal_login(client)

        resp = client.post(
            "/api/auth/impersonate",
            json={"employee_id": ids["teacher_emp_id"], "mode": "write"},
        )
        assert (
            resp.status_code == 403
        ), f"principal 不應有 write 模擬權限，got {resp.status_code}"

    def test_principal_can_readonly_impersonate(self, setup):
        client, ids = setup
        self._principal_login(client)

        resp = client.post(
            "/api/auth/impersonate",
            json={"employee_id": ids["teacher_emp_id"], "mode": "readonly"},
        )
        assert (
            resp.status_code == 200
        ), f"principal 應可 readonly 模擬，got {resp.status_code}: {resp.json()}"

    def test_default_mode_is_readonly(self, setup):
        client, ids = setup
        self._admin_login(client)

        resp = client.post(
            "/api/auth/impersonate",
            json={"employee_id": ids["teacher_emp_id"]},  # 不傳 mode
        )
        assert (
            resp.status_code == 200
        ), f"expected 200, got {resp.status_code}: {resp.json()}"

        token = client.cookies.get("access_token")
        payload = decode_token(token)
        assert (
            payload.get("impersonation_mode") == "readonly"
        ), f"default mode 應為 readonly，實際: {payload.get('impersonation_mode')}"

    def test_cannot_impersonate_admin_preserved(self, setup):
        client, ids = setup
        self._admin_login(client)

        resp = client.post(
            "/api/auth/impersonate",
            json={"employee_id": ids["other_admin_emp_id"], "mode": "readonly"},
        )
        assert resp.status_code == 403, f"不應模擬 admin，got {resp.status_code}"

    def test_cannot_reimpersonate_while_impersonating(self, setup):
        """防巢狀模擬：write mode 模擬 token → 再次 POST impersonate → 409。"""
        client, ids = setup

        # 先以 admin 身份取得 write impersonation token（先登入）
        self._admin_login(client)
        r1 = client.post(
            "/api/auth/impersonate",
            json={"employee_id": ids["teacher_emp_id"], "mode": "write"},
        )
        assert r1.status_code == 200

        # 取得 write impersonation cookie（此時 client cookie jar 已更新為 teacher 的 token）
        write_impersonation_cookie = client.cookies.get("access_token")
        assert write_impersonation_cookie

        # 用 write impersonation token 再嘗試模擬 teacher2 → 應 409
        resp = client.post(
            "/api/auth/impersonate",
            json={"employee_id": ids["teacher2_emp_id"], "mode": "write"},
            cookies={"access_token": write_impersonation_cookie},
        )
        assert (
            resp.status_code == 409
        ), f"巢狀模擬應被 409 拒絕，got {resp.status_code}: {resp.json()}"


# ─── GET /me 回應含 impersonation_mode 欄位 ───────────────────────────────────


class TestGetMeImpersonationMode:
    """AuthUserOut 暴露 impersonation_mode：正常登入 None，模擬中回模式字串。"""

    @pytest.fixture
    def setup(self, app_and_client):
        """建立 admin + teacher，供下方測試使用。"""
        client, session_factory = app_and_client

        with session_factory() as session:
            admin_user = _make_user(
                session,
                username="me_admin",
                role="admin",
                permission_names=["*"],
            )
            teacher_emp = _make_employee(session, "MT001", "測試老師")
            teacher_user = _make_user(
                session,
                employee_id=teacher_emp.id,
                username="me_teacher",
                role="teacher",
            )
            session.commit()
            teacher_user_id = teacher_user.id
            teacher_emp_id = teacher_emp.id

        return client, session_factory, teacher_user_id, teacher_emp_id

    def test_normal_login_get_me_impersonation_mode_is_none(self, setup):
        """正常登入後 GET /me → impersonation_mode 為 None。"""
        client, session_factory, teacher_user_id, teacher_emp_id = setup

        r = _login(client, "me_admin")
        assert r.status_code == 200

        resp = client.get("/api/auth/me")
        assert resp.status_code == 200, f"GET /me failed: {resp.json()}"
        body = resp.json()
        # 欄位存在且值為 None（非模擬情境）
        assert "impersonation_mode" in body, "回應缺 impersonation_mode 欄位"
        assert (
            body["impersonation_mode"] is None
        ), f"非模擬情境 impersonation_mode 應為 None，實際: {body['impersonation_mode']}"

    def test_readonly_impersonate_get_me_impersonation_mode_is_readonly(self, setup):
        """admin readonly 模擬後 GET /me → impersonation_mode == 'readonly'。"""
        from utils.auth import create_access_token

        client, session_factory, teacher_user_id, teacher_emp_id = setup

        # 直接構造帶 impersonation_mode=readonly claim 的 token
        impersonation_token = create_access_token(
            {
                "user_id": teacher_user_id,
                "impersonation_mode": "readonly",
                "impersonated_by": 999,  # 模擬 admin user_id
            }
        )

        # 用此 token 打 GET /me
        resp = client.get(
            "/api/auth/me",
            cookies={"access_token": impersonation_token},
        )
        assert resp.status_code == 200, f"GET /me failed: {resp.json()}"
        body = resp.json()
        assert "impersonation_mode" in body, "回應缺 impersonation_mode 欄位"
        assert (
            body["impersonation_mode"] == "readonly"
        ), f"readonly 模擬 impersonation_mode 應為 'readonly'，實際: {body['impersonation_mode']}"

    def test_impersonate_response_user_has_impersonation_mode(self, setup):
        """POST /api/auth/impersonate 回應的 user dict 含 impersonation_mode。"""
        client, session_factory, teacher_user_id, teacher_emp_id = setup

        r = _login(client, "me_admin")
        assert r.status_code == 200

        resp = client.post(
            "/api/auth/impersonate",
            json={"employee_id": teacher_emp_id, "mode": "readonly"},
        )
        assert resp.status_code == 200, f"impersonate failed: {resp.json()}"
        user_dict = resp.json().get("user", {})
        assert (
            "impersonation_mode" in user_dict
        ), "impersonate 回應 user dict 缺 impersonation_mode"
        assert (
            user_dict["impersonation_mode"] == "readonly"
        ), f"impersonate user.impersonation_mode 應為 'readonly'，實際: {user_dict['impersonation_mode']}"
