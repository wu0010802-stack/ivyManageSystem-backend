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
from api.salary import router as salary_router
from api.portal.attendance import router as portal_attendance_router
from api.portal.leaves import router as portal_leaves_router
from api.portal.schedule import router as portal_schedule_router
from api.portal.overtimes import router as portal_overtimes_router
from api.portal.salary import router as portal_salary_router
from models.database import ApprovalLog, Base, Employee, LeaveRecord, OvertimeRecord, SalaryRecord, User
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
    app.include_router(salary_router)
    app.include_router(portal_attendance_router, prefix="/api/portal")
    app.include_router(portal_leaves_router, prefix="/api/portal")
    app.include_router(portal_schedule_router, prefix="/api/portal")
    app.include_router(portal_overtimes_router, prefix="/api/portal")
    app.include_router(portal_salary_router, prefix="/api/portal")

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


# ─────────────────────────────────────────────────────────────
# HIGH-1a：自我核准假單應回傳 403
# ─────────────────────────────────────────────────────────────

class TestSelfApprovalPrevention:
    """員工（含主管）不可自我核准自己的假單或加班單"""

    def _setup_supervisor_with_leave(self, session_factory):
        """建立主管帳號與一筆待審假單，回傳 (employee_id, leave_id)"""
        with session_factory() as session:
            from datetime import date as _date
            emp = _create_employee(session, "SUP001", "主管甲")
            _create_user(
                session,
                username="supervisor_self",
                password="TempPass123",
                role="supervisor",
                permissions=Permission.LEAVES_WRITE | Permission.LEAVES_READ,
                employee=emp,
            )
            leave = LeaveRecord(
                employee_id=emp.id,
                leave_type="personal",
                start_date=_date(2026, 3, 10),
                end_date=_date(2026, 3, 10),
                leave_hours=8,
                is_approved=None,
            )
            session.add(leave)
            session.commit()
            return emp.id, leave.id

    def _setup_supervisor_with_overtime(self, session_factory):
        """建立主管帳號與一筆待審加班單，回傳 (employee_id, overtime_id)"""
        with session_factory() as session:
            from datetime import date as _date
            emp = _create_employee(session, "SUP002", "主管乙")
            _create_user(
                session,
                username="supervisor_self_ot",
                password="TempPass123",
                role="supervisor",
                permissions=Permission.OVERTIME_WRITE | Permission.OVERTIME_READ,
                employee=emp,
            )
            ot = OvertimeRecord(
                employee_id=emp.id,
                overtime_date=_date(2026, 3, 10),
                overtime_type="weekday",
                hours=2,
                is_approved=None,
            )
            session.add(ot)
            session.commit()
            return emp.id, ot.id

    def test_self_approve_leave_returns_403(self, client_with_db):
        """主管核准自己的假單 → 應回傳 403"""
        client, session_factory = client_with_db
        _, leave_id = self._setup_supervisor_with_leave(session_factory)

        _login(client, "supervisor_self", "TempPass123")
        res = client.put(
            f"/api/leaves/{leave_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 403
        assert "自我核准" in res.json()["detail"]

    def test_approve_other_leave_is_allowed(self, client_with_db):
        """主管核准他人假單 → 不因自我核准被擋（僅測試非 403 路徑）"""
        client, session_factory = client_with_db
        with session_factory() as session:
            from datetime import date as _date
            # 建立主管
            sup = _create_employee(session, "SUP003", "主管丙")
            _create_user(
                session,
                username="supervisor_other",
                password="TempPass123",
                role="supervisor",
                permissions=Permission.LEAVES_WRITE | Permission.LEAVES_READ,
                employee=sup,
            )
            # 建立另一員工的假單
            staff = _create_employee(session, "STA001", "員工甲")
            leave = LeaveRecord(
                employee_id=staff.id,
                leave_type="personal",
                start_date=_date(2026, 3, 10),
                end_date=_date(2026, 3, 10),
                leave_hours=8,
                is_approved=None,
            )
            session.add(leave)
            session.commit()
            leave_id = leave.id

        _login(client, "supervisor_other", "TempPass123")
        res = client.put(
            f"/api/leaves/{leave_id}/approve",
            json={"approved": True},
        )
        # 自我核准守衛不應阻擋（可能因角色資格或其他原因失敗，但不會是因自我核准）
        assert res.status_code != 403 or "自我核准" not in res.json().get("detail", "")

    def test_self_approve_overtime_returns_403(self, client_with_db):
        """主管核准自己的加班單 → 應回傳 403"""
        client, session_factory = client_with_db
        _, ot_id = self._setup_supervisor_with_overtime(session_factory)

        _login(client, "supervisor_self_ot", "TempPass123")
        res = client.put(
            f"/api/overtimes/{ot_id}/approve",
            params={"approved": True},
        )
        assert res.status_code == 403
        assert "自我核准" in res.json()["detail"]


# ─────────────────────────────────────────────────────────────
# MEDIUM-3：薪資記錄非 admin/hr 帳號只能看自己
# ─────────────────────────────────────────────────────────────

class TestSalaryRecordsRoleFilter:
    """非 admin/hr 帳號呼叫 GET /salaries/records 只能看到自己的資料"""

    def test_non_admin_sees_only_own_salary(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            emp_a = _create_employee(session, "E001", "員工A")
            emp_b = _create_employee(session, "E002", "員工B")
            _create_user(
                session,
                username="staff_user",
                password="TempPass123",
                role="supervisor",
                permissions=Permission.SALARY_READ,
                employee=emp_a,
            )
            session.add(SalaryRecord(
                employee_id=emp_a.id, salary_year=2026, salary_month=3,
                base_salary=30000,
            ))
            session.add(SalaryRecord(
                employee_id=emp_b.id, salary_year=2026, salary_month=3,
                base_salary=32000,
            ))
            session.commit()

        _login(client, "staff_user", "TempPass123")
        res = client.get("/api/salaries/records", params={"year": 2026, "month": 3})
        assert res.status_code == 200
        data = res.json()
        # 只能看到自己的一筆
        assert len(data) == 1
        assert data[0]["employee_code"] == "E001"

    def test_admin_sees_all_salaries(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            emp_a = _create_employee(session, "A001", "員工A2")
            emp_b = _create_employee(session, "A002", "員工B2")
            _create_user(
                session,
                username="admin_user",
                password="TempPass123",
                role="admin",
                permissions=-1,
            )
            session.add(SalaryRecord(
                employee_id=emp_a.id, salary_year=2026, salary_month=4,
                base_salary=30000,
            ))
            session.add(SalaryRecord(
                employee_id=emp_b.id, salary_year=2026, salary_month=4,
                base_salary=32000,
            ))
            session.commit()

        _login(client, "admin_user", "TempPass123")
        res = client.get("/api/salaries/records", params={"year": 2026, "month": 4})
        assert res.status_code == 200
        assert len(res.json()) == 2


# ─────────────────────────────────────────────────────────────
# LOW-4：Portal 端點日期邊界驗證
# ─────────────────────────────────────────────────────────────

class TestPortalDateBoundaryValidation:
    """Portal 端點 year/month 越界應回傳 422（FastAPI 的 Query 驗證層攔截，無需 DB）"""

    def test_invalid_year_and_month_return_422(self, client_with_db):
        """傳入越界 year / month 參數 → 422 Unprocessable Entity"""
        client, session_factory = client_with_db
        with session_factory() as session:
            emp = _create_employee(session, "PRT001", "教師甲")
            _create_user(
                session,
                username="portal_user",
                password="TempPass123",
                role="teacher",
                permissions=0,
                employee=emp,
            )
            session.commit()
        _login(client, "portal_user", "TempPass123")

        endpoints = [
            ("/api/portal/attendance-sheet", {"year": 1900, "month": 3}),
            ("/api/portal/my-leaves", {"year": 9999, "month": 3}),
            ("/api/portal/my-schedule", {"year": 2026, "month": 13}),
            ("/api/portal/my-overtimes", {"year": 2026, "month": 0}),
            ("/api/portal/salary-preview", {"year": 1999, "month": 6}),
        ]
        for url, params in endpoints:
            res = client.get(url, params=params)
            assert res.status_code == 422, f"{url} 應回傳 422，實際 {res.status_code}"
