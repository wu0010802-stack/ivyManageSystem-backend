"""安全性回歸測試。"""

import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.approval_settings import router as approval_settings_router
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.leaves import router as leaves_router
from api.overtimes import router as overtimes_router
from models.database import ApprovalLog, Base, Employee, User
from utils.auth import hash_password
from utils.permissions import Permission, get_role_default_permissions, has_permission


@pytest.fixture
def client_with_db(tmp_path):
    """建立隔離的 sqlite 測試 app。"""
    db_path = tmp_path / "security-regressions.sqlite"
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
    app.include_router(leaves_router)
    app.include_router(overtimes_router)
    app.include_router(approval_settings_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_employee(session, employee_id: str, name: str) -> Employee:
    employee = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=32000,
        hire_date=None,
        is_active=True,
    )
    session.add(employee)
    session.flush()
    return employee


def _create_user(
    session,
    *,
    username: str,
    password: str,
    role: str,
    permissions: int | None = None,
    employee: Employee | None = None,
    must_change_password: bool = False,
) -> User:
    user = User(
        employee_id=employee.id if employee else None,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=permissions,
        must_change_password=must_change_password,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )


class TestTeacherPermissionRegression:
    def test_teacher_default_permissions_do_not_include_management_leave_or_overtime(self):
        permissions = get_role_default_permissions("teacher")

        assert not has_permission(permissions, Permission.LEAVES_READ)
        assert not has_permission(permissions, Permission.LEAVES_WRITE)
        assert not has_permission(permissions, Permission.OVERTIME_READ)
        assert not has_permission(permissions, Permission.OVERTIME_WRITE)

    def test_teacher_with_legacy_permissions_still_cannot_call_management_leave_or_overtime_api(
        self, client_with_db
    ):
        client, session_factory = client_with_db
        with session_factory() as session:
            employee = _create_employee(session, "T001", "教師甲")
            employee_id = employee.id
            _create_user(
                session,
                username="teacher_legacy",
                password="TempPass123",
                role="teacher",
                employee=employee,
                permissions=(
                    Permission.LEAVES_READ
                    | Permission.LEAVES_WRITE
                    | Permission.OVERTIME_READ
                    | Permission.OVERTIME_WRITE
                ),
            )
            session.commit()

        login_res = _login(client, "teacher_legacy", "TempPass123")
        assert login_res.status_code == 200

        leaves_res = client.get("/api/leaves", params={"year": 2026, "month": 3})
        assert leaves_res.status_code == 403

        overtime_res = client.post(
            "/api/overtimes",
            json={
                "employee_id": employee_id,
                "overtime_date": "2026-03-12",
                "overtime_type": "weekday",
                "hours": 2,
            },
        )
        assert overtime_res.status_code == 403


class TestMustChangePasswordEnforcement:
    def test_must_change_password_blocks_other_api_until_password_is_changed(
        self, client_with_db
    ):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(
                session,
                username="admin_temp",
                password="TempPass123",
                role="admin",
                permissions=-1,
                must_change_password=True,
            )
            session.commit()

        login_res = _login(client, "admin_temp", "TempPass123")
        assert login_res.status_code == 200
        assert login_res.json()["must_change_password"] is True

        blocked_res = client.get("/api/auth/users")
        assert blocked_res.status_code == 403
        assert "修改密碼" in blocked_res.json()["detail"]

        change_res = client.post(
            "/api/auth/change-password",
            json={
                "old_password": "TempPass123",
                "new_password": "ChangedPass123",
            },
        )
        assert change_res.status_code == 200

        allowed_res = client.get("/api/auth/users")
        assert allowed_res.status_code == 200

    def test_refresh_is_blocked_while_password_change_is_required(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(
                session,
                username="admin_refresh",
                password="TempPass123",
                role="admin",
                permissions=-1,
                must_change_password=True,
            )
            session.commit()

        login_res = _login(client, "admin_refresh", "TempPass123")
        assert login_res.status_code == 200

        refresh_res = client.post("/api/auth/refresh")
        assert refresh_res.status_code == 403


class TestApprovalLogAccessControl:
    def test_non_admin_must_specify_doc_type_and_hold_matching_permission(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            employee = _create_employee(session, "HR001", "人事甲")
            _create_user(
                session,
                username="hr_leave_only",
                password="TempPass123",
                role="hr",
                employee=employee,
                permissions=Permission.LEAVES_READ,
            )
            session.add_all(
                [
                    ApprovalLog(
                        doc_type="leave",
                        doc_id=1,
                        action="approved",
                        approver_username="admin",
                        approver_role="admin",
                    ),
                    ApprovalLog(
                        doc_type="overtime",
                        doc_id=2,
                        action="approved",
                        approver_username="admin",
                        approver_role="admin",
                    ),
                ]
            )
            session.commit()

        login_res = _login(client, "hr_leave_only", "TempPass123")
        assert login_res.status_code == 200

        missing_type_res = client.get("/api/approval-settings/logs")
        assert missing_type_res.status_code == 400

        leave_res = client.get("/api/approval-settings/logs", params={"doc_type": "leave"})
        assert leave_res.status_code == 200
        assert [row["doc_type"] for row in leave_res.json()] == ["leave"]

        overtime_res = client.get("/api/approval-settings/logs", params={"doc_type": "overtime"})
        assert overtime_res.status_code == 403
