"""IDOR audit Phase 2：F-032 — exports/employee-attendance 缺自我守衛。

PoC 描述：`GET /api/exports/employee-attendance?employee_id=...&year=...&month=...`
endpoint 只受 `require_staff_permission(Permission.ATTENDANCE_READ)` 守門，
未檢查 `employee_id` 是否為呼叫者本人，也未要求 admin/hr 角色才能查他人。
任何持 ATTENDANCE_READ 的角色（含預設 supervisor/hr 與自訂助理角色）即可下載
任意員工的逐日打卡 + 請假 + 加班明細 Excel。

修補：endpoint 入口呼叫 `utils.salary_access.enforce_self_or_full_salary`
（admin/hr 全可、其他角色僅自己）。helper 名雖帶 salary，但其角色語義
（FULL_SALARY_ROLES = admin/hr）正好對應出勤匯出守衛需求。

涵蓋情境：
- supervisor 看他人 → 403
- supervisor 看自己 → 200
- admin 看任何人 → 200
- hr 看任何人 → 200
- 純 admin 帳號（無 employee_id）看任何人 → 200（None-safe path）
- 自訂角色 + ATTENDANCE_READ 看他人 → 403（key adversarial）
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
import api.exports as exports_module
from api.exports import router as exports_router
from datetime import date
from models.database import Base, Employee, User
from utils.auth import hash_password
from utils.permissions import Permission

# ─────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def attendance_export_client(tmp_path):
    db_path = tmp_path / "f032.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
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
    app.include_router(exports_router)

    # 測試環境停用匯出限流（5 次/分鐘）以免 6 個案例觸發 429。
    app.dependency_overrides[exports_module._export_rate_limit] = lambda: None

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
    password="Pass1234",
    role,
    permissions,
    employee_id=None,
):
    user = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=int(permissions),
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password="Pass1234"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text


def _create_employee(session, employee_id_str: str, name: str) -> Employee:
    emp = Employee(
        employee_id=employee_id_str,
        name=name,
        base_salary=30000,
        hire_date=date(2024, 1, 1),
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


# ─────────────────────────────────────────────────────────────────────────
# F-032：GET /exports/employee-attendance
# ─────────────────────────────────────────────────────────────────────────


class TestF032_EmployeeAttendanceExport:
    """非 admin/hr 不可下載他人逐日打卡明細。"""

    def test_supervisor_cannot_export_other_employee_attendance(
        self, attendance_export_client
    ):
        client, sf = attendance_export_client
        with sf() as s:
            self_emp = _create_employee(s, "F032_self", "本人")
            other_emp = _create_employee(s, "F032_other", "他人")
            _create_user(
                s,
                username="sv_cross",
                role="supervisor",
                permissions=int(Permission.ATTENDANCE_READ),
                employee_id=self_emp.id,
            )
            s.commit()
            other_id = other_emp.id

        _login(client, "sv_cross")
        res = client.get(
            f"/api/exports/employee-attendance?employee_id={other_id}&year=2026&month=4"
        )
        assert res.status_code == 403, res.text

    def test_supervisor_can_export_own_attendance(self, attendance_export_client):
        client, sf = attendance_export_client
        with sf() as s:
            self_emp = _create_employee(s, "F032_self2", "本人2")
            _create_user(
                s,
                username="sv_self",
                role="supervisor",
                permissions=int(Permission.ATTENDANCE_READ),
                employee_id=self_emp.id,
            )
            s.commit()
            self_id = self_emp.id

        _login(client, "sv_self")
        res = client.get(
            f"/api/exports/employee-attendance?employee_id={self_id}&year=2026&month=4"
        )
        assert res.status_code == 200, res.text

    def test_admin_can_export_any(self, attendance_export_client):
        client, sf = attendance_export_client
        with sf() as s:
            target = _create_employee(s, "F032_anyA", "目標A")
            # admin 帳號通常綁 admin 員工本人，但這裡也測 admin 看任意他人
            _create_user(
                s,
                username="adm_f032",
                role="admin",
                permissions=-1,
            )
            s.commit()
            target_id = target.id

        _login(client, "adm_f032")
        res = client.get(
            f"/api/exports/employee-attendance?employee_id={target_id}&year=2026&month=4"
        )
        assert res.status_code == 200, res.text

    def test_hr_can_export_any(self, attendance_export_client):
        client, sf = attendance_export_client
        with sf() as s:
            target = _create_employee(s, "F032_anyH", "目標H")
            _create_user(
                s,
                username="hr_f032",
                role="hr",
                permissions=int(Permission.ATTENDANCE_READ),
            )
            s.commit()
            target_id = target.id

        _login(client, "hr_f032")
        res = client.get(
            f"/api/exports/employee-attendance?employee_id={target_id}&year=2026&month=4"
        )
        assert res.status_code == 200, res.text

    def test_pure_admin_account_without_employee_id_can_export_any(
        self, attendance_export_client
    ):
        """純 admin 帳號（未綁 employee_id）對任意 employee_id 仍可匯出 — 走 None-safe path。"""
        client, sf = attendance_export_client
        with sf() as s:
            target = _create_employee(s, "F032_anyP", "目標P")
            _create_user(
                s,
                username="adm_pure",
                role="admin",
                permissions=-1,
                employee_id=None,
            )
            s.commit()
            target_id = target.id

        _login(client, "adm_pure")
        res = client.get(
            f"/api/exports/employee-attendance?employee_id={target_id}&year=2026&month=4"
        )
        assert res.status_code == 200, res.text

    def test_custom_role_with_attendance_read_only_cannot_export_other(
        self, attendance_export_client
    ):
        """關鍵 adversarial：自訂角色（非 admin/hr）即使持 ATTENDANCE_READ 也不可看他人。"""
        client, sf = attendance_export_client
        with sf() as s:
            self_emp = _create_employee(s, "F032_cself", "本人C")
            other_emp = _create_employee(s, "F032_cother", "他人C")
            _create_user(
                s,
                username="custom_att_assist",
                role="attendance_assist",
                permissions=int(Permission.ATTENDANCE_READ),
                employee_id=self_emp.id,
            )
            s.commit()
            other_id = other_emp.id

        _login(client, "custom_att_assist")
        res = client.get(
            f"/api/exports/employee-attendance?employee_id={other_id}&year=2026&month=4"
        )
        assert res.status_code == 403, res.text
