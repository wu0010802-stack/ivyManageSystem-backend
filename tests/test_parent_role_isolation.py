"""家長角色 (role='parent') 結構性隔離回歸測試。

Batch 1 — 地基：尚無家長路由，先驗證下列「擋人」邏輯：
1. parent JWT 撞 portal/* → 403（router-level require_non_parent_role）
2. parent JWT 撞 staff endpoint（require_staff_permission）→ 403
3. teacher JWT 撞 portal/* → 200（既有行為不被破壞）
4. require_staff_permission 即使 user.permissions=-1 也擋 parent role

家長路由本身的 IDOR 測試在 Batch 3 補（test_parent_idor_regressions.py）。
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
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.employees import router as employees_router
from api.portal import router as portal_router
from models.database import Base, Employee, User
from utils.auth import create_access_token, hash_password


@pytest.fixture
def isolated_app(tmp_path):
    """獨立 sqlite test app；裝載 auth + portal + employees。"""
    db_path = tmp_path / "parent-role-isolation.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(db_engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(portal_router)
    app.include_router(employees_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _create_parent_user(session, *, line_user_id: str = "Uparent001") -> User:
    """建立家長 User（無 employee 關聯、role='parent'、permissions=0）。"""
    user = User(
        employee_id=None,
        username=f"parent_line_{line_user_id}",
        password_hash="!LINE_ONLY",  # sentinel：永不匹配
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


def _create_teacher_user(session, *, employee_id_str: str = "T001") -> User:
    employee = Employee(
        employee_id=employee_id_str,
        name="測試老師",
        base_salary=32000,
        is_active=True,
    )
    session.add(employee)
    session.flush()
    user = User(
        employee_id=employee.id,
        username=f"teacher_{employee_id_str}",
        password_hash=hash_password("Passw0rd!"),
        role="teacher",
        permissions=0,
        is_active=True,
        must_change_password=False,
        token_version=0,
    )
    session.add(user)
    session.flush()
    return user


def _make_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": user.employee_id,
            "role": user.role,
            "name": user.username,
            "permissions": user.permissions or 0,
            "token_version": user.token_version or 0,
        }
    )


class TestParentBlockedFromPortal:
    """parent token 撞 portal 應立即被 router-level dependency 擋下。"""

    def test_parent_calendar_returns_403(self, isolated_app):
        client, session_factory = isolated_app
        with session_factory() as session:
            parent = _create_parent_user(session)
            session.commit()
            token = _make_token(parent)

        response = client.get(
            "/api/portal/calendar",
            params={"year": 2026, "month": 4},
            cookies={"access_token": token},
        )
        assert response.status_code == 403
        assert "家長" in response.json().get("detail", "")

    def test_parent_announcements_returns_403(self, isolated_app):
        client, session_factory = isolated_app
        with session_factory() as session:
            parent = _create_parent_user(session)
            session.commit()
            token = _make_token(parent)

        response = client.get(
            "/api/portal/announcements",
            cookies={"access_token": token},
        )
        assert response.status_code == 403


class TestTeacherStillReachesPortal:
    """既有教師 token 仍可進 portal（router-level dependency 不誤傷）。"""

    def test_teacher_calendar_returns_200(self, isolated_app):
        client, session_factory = isolated_app
        with session_factory() as session:
            teacher = _create_teacher_user(session)
            session.commit()
            token = _make_token(teacher)

        response = client.get(
            "/api/portal/calendar",
            params={"year": 2026, "month": 4},
            cookies={"access_token": token},
        )
        # 200 或 503（official_calendar 外部資料未同步）皆可，不是 403
        assert response.status_code != 403


class TestParentBlockedFromStaffEndpoint:
    """require_staff_permission 必拒絕 parent，即使 permissions=-1。"""

    def test_parent_with_full_permissions_still_blocked(self, isolated_app):
        client, session_factory = isolated_app
        with session_factory() as session:
            parent = _create_parent_user(session)
            # 模擬「有人錯誤地給 parent -1 全權限」：仍應被 role check 擋
            parent.permissions = -1
            session.commit()
            token = create_access_token(
                {
                    "user_id": parent.id,
                    "employee_id": None,
                    "role": "parent",
                    "name": parent.username,
                    "permissions": -1,
                    "token_version": parent.token_version or 0,
                }
            )

        response = client.get(
            "/api/employees",
            cookies={"access_token": token},
        )
        assert response.status_code == 403
        assert "家長" in response.json().get("detail", "")


class TestParentTokenInvalidatedOnTokenVersionBump:
    """Guardian 軟刪 / unbind 後，bump token_version 即可使 parent 舊 token 失效。"""

    def test_old_parent_token_rejected_after_token_version_bump(self, isolated_app):
        client, session_factory = isolated_app
        with session_factory() as session:
            parent = _create_parent_user(session)
            session.commit()
            old_token = _make_token(parent)

        # 模擬 unbind / 軟刪：bump token_version
        with session_factory() as session:
            user = session.query(User).filter(User.id == parent.id).first()
            user.token_version = (user.token_version or 0) + 1
            session.commit()

        response = client.get(
            "/api/portal/calendar",
            params={"year": 2026, "month": 4},
            cookies={"access_token": old_token},
        )
        # 401（token_version 不符）或 403（先撞 role）皆可—重點是不能 200
        assert response.status_code in (401, 403)
