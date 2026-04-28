"""
回歸測試：USER_MANAGEMENT_WRITE 提權鏈守衛（F-037 ~ F-040）

修補目標：
    在 api/auth.py 為 4 支使用者管理端點加上 caller-relative 守衛，避免
    任何持有 USER_MANAGEMENT_WRITE 但非 admin 的角色：
    - F-037 POST /api/auth/users           建立 role=admin 或授予超出自身的權限
    - F-038 PUT  /api/auth/users/{id}      自我升 admin 或修改其他 admin
    - F-039 PUT  /api/auth/users/{id}/reset-password  重設 admin 密碼後接管
    - F-040 DELETE /api/auth/users/{id}    硬刪其他 admin

守衛邏輯（_assert_can_manage_user）：
    1. caller 是 admin → 一律放行
    2. target.role == "admin" 且 caller 不是 admin → 拒絕
    3. payload.role == "admin" 且 caller 不是 admin → 拒絕
    4. payload 最終權限有 caller 沒有的 bit → 拒絕
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
from api.auth import router as auth_router, _account_failures, _ip_attempts
from models.database import Base, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def auth_client(tmp_path):
    """建立隔離的 sqlite 測試 app（user management authz 用）。"""
    db_path = tmp_path / "user-mgmt-authz.sqlite"
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


def _create_user(
    session,
    *,
    username,
    password,
    role,
    permissions,
    employee_id=None,
    is_active=True,
):
    user = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=permissions,
        is_active=is_active,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


# 「非 admin 但持有 USER_MANAGEMENT_WRITE」的權限組合（hr 自訂）
NON_ADMIN_PERMS = int(Permission.USER_MANAGEMENT_WRITE) | int(Permission.EMPLOYEES_READ)


# ====================================================================
# F-037: POST /api/auth/users
# ====================================================================


class TestCreateUser:
    """F-037：非 admin 持 USER_MANAGEMENT_WRITE 不可建立 admin / 授超權帳號。"""

    def test_non_admin_with_um_write_creates_admin_role(self, auth_client):
        """非 admin caller 嘗試建立 role=admin 應 403。"""
        client, session_factory = auth_client
        with session_factory() as session:
            _create_user(
                session,
                username="hr_user",
                password="HrPass1234",
                role="hr",
                permissions=NON_ADMIN_PERMS,
            )
            session.commit()

        assert _login(client, "hr_user", "HrPass1234").status_code == 200

        res = client.post(
            "/api/auth/users",
            json={
                "username": "ghost_admin",
                "password": "Strong!@#1234",
                "role": "admin",
                "permissions": int(Permission.USER_MANAGEMENT_WRITE),
            },
        )
        assert res.status_code == 403
        assert "admin" in res.json()["detail"]

    def test_non_admin_with_um_write_grants_full_permissions(self, auth_client):
        """非 admin caller 嘗試授予 -1（全權）應 403。"""
        client, session_factory = auth_client
        with session_factory() as session:
            _create_user(
                session,
                username="hr_user2",
                password="HrPass1234",
                role="hr",
                permissions=NON_ADMIN_PERMS,
            )
            session.commit()

        assert _login(client, "hr_user2", "HrPass1234").status_code == 200

        res = client.post(
            "/api/auth/users",
            json={
                "username": "fake_hr",
                "password": "Strong!@#1234",
                "role": "hr",
                "permissions": -1,
            },
        )
        assert res.status_code == 403
        assert "超出" in res.json()["detail"]

    def test_non_admin_with_um_write_creates_same_or_lower_role_perms(
        self, auth_client
    ):
        """非 admin caller 建立同等或更低權限帳號應成功（201）。"""
        client, session_factory = auth_client
        with session_factory() as session:
            _create_user(
                session,
                username="hr_user3",
                password="HrPass1234",
                role="hr",
                permissions=NON_ADMIN_PERMS,
            )
            session.commit()

        assert _login(client, "hr_user3", "HrPass1234").status_code == 200

        # 建立帳號權限 = caller 權限的子集（只給 EMPLOYEES_READ）
        res = client.post(
            "/api/auth/users",
            json={
                "username": "lower_user",
                "password": "Strong!@#1234",
                "role": "hr",
                "permissions": int(Permission.EMPLOYEES_READ),
            },
        )
        assert res.status_code == 201, f"預期 201，實際 {res.status_code}: {res.json()}"

    def test_admin_creates_admin(self, auth_client):
        """admin caller 建立 admin 帳號應成功（201）。"""
        client, session_factory = auth_client
        with session_factory() as session:
            _create_user(
                session,
                username="root_admin",
                password="AdminPass1234",
                role="admin",
                permissions=-1,
            )
            session.commit()

        assert _login(client, "root_admin", "AdminPass1234").status_code == 200

        res = client.post(
            "/api/auth/users",
            json={
                "username": "new_admin",
                "password": "Strong!@#1234",
                "role": "admin",
                "permissions": -1,
            },
        )
        assert res.status_code == 201, f"預期 201，實際 {res.status_code}: {res.json()}"


# ====================================================================
# F-038: PUT /api/auth/users/{user_id}
# ====================================================================


class TestUpdateUser:
    """F-038：非 admin 不可改 admin、不可自我升 admin、不可授超權。"""

    def test_non_admin_updates_target_admin(self, auth_client):
        """非 admin caller 嘗試修改 admin 帳號應 403。"""
        client, session_factory = auth_client
        with session_factory() as session:
            _create_user(
                session,
                username="hr_u",
                password="HrPass1234",
                role="hr",
                permissions=NON_ADMIN_PERMS,
            )
            target = _create_user(
                session,
                username="root_admin",
                password="AdminPass1234",
                role="admin",
                permissions=-1,
            )
            session.commit()
            target_id = target.id

        assert _login(client, "hr_u", "HrPass1234").status_code == 200

        res = client.put(
            f"/api/auth/users/{target_id}",
            json={"permissions": 0, "is_active": False},
        )
        assert res.status_code == 403
        assert "管理員" in res.json()["detail"]

    def test_non_admin_self_promotes_to_admin(self, auth_client):
        """非 admin 將自己 role 改為 admin 應 403。"""
        client, session_factory = auth_client
        with session_factory() as session:
            self_user = _create_user(
                session,
                username="self_promote",
                password="HrPass1234",
                role="hr",
                permissions=NON_ADMIN_PERMS,
            )
            session.commit()
            self_id = self_user.id

        assert _login(client, "self_promote", "HrPass1234").status_code == 200

        res = client.put(
            f"/api/auth/users/{self_id}",
            json={"role": "admin", "permissions": -1},
        )
        assert res.status_code == 403
        assert "admin" in res.json()["detail"]

    def test_non_admin_grants_payload_permissions_exceeding_own(self, auth_client):
        """非 admin 授予的 permissions 超出自身範圍應 403。"""
        client, session_factory = auth_client
        with session_factory() as session:
            _create_user(
                session,
                username="hr_u2",
                password="HrPass1234",
                role="hr",
                permissions=NON_ADMIN_PERMS,
            )
            target = _create_user(
                session,
                username="some_teacher",
                password="TchPass1234",
                role="teacher",
                permissions=int(Permission.EMPLOYEES_READ),
            )
            session.commit()
            target_id = target.id

        assert _login(client, "hr_u2", "HrPass1234").status_code == 200

        # caller 僅持 USER_MANAGEMENT_WRITE | EMPLOYEES_READ；此處嘗試授予 -1
        res = client.put(
            f"/api/auth/users/{target_id}",
            json={"permissions": -1},
        )
        assert res.status_code == 403
        assert "超出" in res.json()["detail"]

    def test_non_admin_updates_same_role_target_with_subset_perms(self, auth_client):
        """非 admin 修改同等角色 target，授予自身權限子集應成功（200）。"""
        client, session_factory = auth_client
        with session_factory() as session:
            _create_user(
                session,
                username="hr_u3",
                password="HrPass1234",
                role="hr",
                permissions=NON_ADMIN_PERMS,
            )
            target = _create_user(
                session,
                username="some_hr",
                password="HrPass1234",
                role="hr",
                permissions=int(Permission.EMPLOYEES_READ),
            )
            session.commit()
            target_id = target.id

        assert _login(client, "hr_u3", "HrPass1234").status_code == 200

        res = client.put(
            f"/api/auth/users/{target_id}",
            json={"permissions": int(Permission.EMPLOYEES_READ)},
        )
        assert res.status_code == 200, f"預期 200，實際 {res.status_code}: {res.json()}"

    def test_existing_self_disable_block_still_fires(self, auth_client):
        """既有「不可停用自己」守衛仍生效（400）。"""
        client, session_factory = auth_client
        with session_factory() as session:
            admin_user = _create_user(
                session,
                username="root_admin_x",
                password="AdminPass1234",
                role="admin",
                permissions=-1,
            )
            session.commit()
            admin_id = admin_user.id

        assert _login(client, "root_admin_x", "AdminPass1234").status_code == 200

        res = client.put(
            f"/api/auth/users/{admin_id}",
            json={"is_active": False},
        )
        assert res.status_code == 400
        assert "停用自己" in res.json()["detail"]

    def test_successful_update_still_bumps_token_version(self, auth_client):
        """成功修改 role/permissions 後 target.token_version 仍會 +1。"""
        client, session_factory = auth_client
        with session_factory() as session:
            _create_user(
                session,
                username="root_admin_y",
                password="AdminPass1234",
                role="admin",
                permissions=-1,
            )
            target = _create_user(
                session,
                username="some_target",
                password="TgtPass1234",
                role="teacher",
                permissions=int(Permission.EMPLOYEES_READ),
            )
            session.commit()
            target_id = target.id
            old_token_version = target.token_version or 0

        assert _login(client, "root_admin_y", "AdminPass1234").status_code == 200

        res = client.put(
            f"/api/auth/users/{target_id}",
            json={"role": "hr", "permissions": int(Permission.EMPLOYEES_READ)},
        )
        assert res.status_code == 200, f"預期 200，實際 {res.status_code}: {res.json()}"

        # 確認 token_version 已遞增
        with session_factory() as session:
            refreshed = session.query(User).filter(User.id == target_id).first()
            assert (refreshed.token_version or 0) == old_token_version + 1


# ====================================================================
# F-039: PUT /api/auth/users/{user_id}/reset-password
# ====================================================================


class TestResetPassword:
    """F-039：非 admin 不可重設 admin 密碼。"""

    def test_non_admin_resets_admin_password(self, auth_client):
        """非 admin caller 嘗試重設 admin 密碼應 403。"""
        client, session_factory = auth_client
        with session_factory() as session:
            _create_user(
                session,
                username="hr_reset",
                password="HrPass1234",
                role="hr",
                permissions=NON_ADMIN_PERMS,
            )
            target = _create_user(
                session,
                username="root_admin_r",
                password="AdminPass1234",
                role="admin",
                permissions=-1,
            )
            session.commit()
            target_id = target.id

        assert _login(client, "hr_reset", "HrPass1234").status_code == 200

        res = client.put(
            f"/api/auth/users/{target_id}/reset-password",
            json={"new_password": "Pwned!23456"},
        )
        assert res.status_code == 403
        assert "管理員" in res.json()["detail"]

    def test_non_admin_resets_non_admin_password(self, auth_client):
        """非 admin caller 重設非 admin 帳號密碼應成功（200）。"""
        client, session_factory = auth_client
        with session_factory() as session:
            _create_user(
                session,
                username="hr_reset2",
                password="HrPass1234",
                role="hr",
                permissions=NON_ADMIN_PERMS,
            )
            target = _create_user(
                session,
                username="some_teacher_r",
                password="TchPass1234",
                role="teacher",
                permissions=int(Permission.EMPLOYEES_READ),
            )
            session.commit()
            target_id = target.id

        assert _login(client, "hr_reset2", "HrPass1234").status_code == 200

        res = client.put(
            f"/api/auth/users/{target_id}/reset-password",
            json={"new_password": "NewPass!23456"},
        )
        assert res.status_code == 200, f"預期 200，實際 {res.status_code}: {res.json()}"

    def test_admin_resets_any_password(self, auth_client):
        """admin caller 重設任何帳號密碼（含 admin）應成功（200）。"""
        client, session_factory = auth_client
        with session_factory() as session:
            _create_user(
                session,
                username="root_admin_z",
                password="AdminPass1234",
                role="admin",
                permissions=-1,
            )
            target = _create_user(
                session,
                username="other_admin",
                password="OtherPass1234",
                role="admin",
                permissions=-1,
            )
            session.commit()
            target_id = target.id

        assert _login(client, "root_admin_z", "AdminPass1234").status_code == 200

        res = client.put(
            f"/api/auth/users/{target_id}/reset-password",
            json={"new_password": "ResetPass!2345"},
        )
        assert res.status_code == 200, f"預期 200，實際 {res.status_code}: {res.json()}"


# ====================================================================
# F-040: DELETE /api/auth/users/{user_id}
# ====================================================================


class TestDeleteUser:
    """F-040：非 admin 不可硬刪 admin 帳號。"""

    def test_non_admin_deletes_admin(self, auth_client):
        """非 admin caller 刪 admin 應 403。"""
        client, session_factory = auth_client
        with session_factory() as session:
            _create_user(
                session,
                username="hr_del",
                password="HrPass1234",
                role="hr",
                permissions=NON_ADMIN_PERMS,
            )
            target = _create_user(
                session,
                username="root_admin_d",
                password="AdminPass1234",
                role="admin",
                permissions=-1,
            )
            session.commit()
            target_id = target.id

        assert _login(client, "hr_del", "HrPass1234").status_code == 200

        res = client.delete(f"/api/auth/users/{target_id}")
        assert res.status_code == 403
        assert "管理員" in res.json()["detail"]

    def test_non_admin_deletes_non_admin(self, auth_client):
        """非 admin caller 刪非 admin 帳號應成功（200）。"""
        client, session_factory = auth_client
        with session_factory() as session:
            _create_user(
                session,
                username="hr_del2",
                password="HrPass1234",
                role="hr",
                permissions=NON_ADMIN_PERMS,
            )
            target = _create_user(
                session,
                username="del_target",
                password="TgtPass1234",
                role="teacher",
                permissions=int(Permission.EMPLOYEES_READ),
            )
            session.commit()
            target_id = target.id

        assert _login(client, "hr_del2", "HrPass1234").status_code == 200

        res = client.delete(f"/api/auth/users/{target_id}")
        assert res.status_code == 200, f"預期 200，實際 {res.status_code}: {res.json()}"

    def test_existing_self_delete_block_still_fires(self, auth_client):
        """既有「不可刪除自己」守衛仍生效（400）。"""
        client, session_factory = auth_client
        with session_factory() as session:
            admin_user = _create_user(
                session,
                username="self_del_admin",
                password="AdminPass1234",
                role="admin",
                permissions=-1,
            )
            session.commit()
            admin_id = admin_user.id

        assert _login(client, "self_del_admin", "AdminPass1234").status_code == 200

        res = client.delete(f"/api/auth/users/{admin_id}")
        assert res.status_code == 400
        assert "刪除自己" in res.json()["detail"]
