"""請假與加班邏輯漏洞回歸測試。"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.overtimes as overtimes_module
import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.leaves import router as leaves_router
from api.overtimes import router as overtimes_router
from api.portal.leaves import router as portal_leaves_router
from models.database import Base, Employee, LeaveQuota, LeaveRecord, OvertimeRecord, User
from utils.auth import hash_password


@pytest.fixture
def leave_overtime_client(tmp_path, monkeypatch):
    """建立隔離的 sqlite 測試 app。"""
    db_path = tmp_path / "leave-overtime-regressions.sqlite"
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

    fake_salary_engine = MagicMock()
    monkeypatch.setattr(overtimes_module, "_salary_engine", fake_salary_engine)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(leaves_router)
    app.include_router(overtimes_router)
    app.include_router(portal_leaves_router, prefix="/api/portal")

    with TestClient(app) as client:
        yield client, session_factory, fake_salary_engine

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_employee(session, employee_id: str, name: str) -> Employee:
    employee = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=36000,
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
    permissions: int,
    employee: Employee | None = None,
) -> User:
    user = User(
        employee_id=employee.id if employee else None,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=permissions,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


class TestPortalLeaveDeductionRatio:
    def test_portal_leave_persists_deduction_ratio_from_leave_type(self, leave_overtime_client):
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "T001", "教師甲")
            _create_user(
                session,
                username="teacher_portal",
                password="PortalPass123",
                role="teacher",
                permissions=0,
                employee=employee,
            )
            session.commit()

        login_res = _login(client, "teacher_portal", "PortalPass123")
        assert login_res.status_code == 200

        create_res = client.post(
            "/api/portal/my-leaves",
            json={
                "leave_type": "annual",
                "start_date": "2026-03-12",
                "end_date": "2026-03-12",
                "leave_hours": 8,
                "reason": "特休",
            },
        )
        assert create_res.status_code == 201

        with session_factory() as session:
            leave = session.query(LeaveRecord).one()
            assert leave.deduction_ratio == 0.0
            assert leave.is_deductible is False

    def test_leave_longer_than_two_days_requires_attachment_before_approval(self, leave_overtime_client):
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "T002", "教師乙")
            leave = LeaveRecord(
                employee_id=employee.id,
                leave_type="personal",
                start_date=date(2026, 3, 12),
                end_date=date(2026, 3, 14),
                leave_hours=24,
                is_approved=None,
            )
            session.add(leave)
            _create_user(
                session,
                username="admin_leave_approve",
                password="AdminPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()
            leave_id = leave.id

        login_res = _login(client, "admin_leave_approve", "AdminPass123")
        assert login_res.status_code == 200

        approve_res = client.put(
            f"/api/leaves/{leave_id}/approve",
            json={"approved": True},
        )
        assert approve_res.status_code == 400
        assert "超過 2 天" in approve_res.json()["detail"]


class TestApprovedOvertimeRollback:
    def test_update_approved_overtime_revokes_comp_leave_and_recalculates_salary(self, leave_overtime_client):
        client, session_factory, fake_salary_engine = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "E001", "員工甲")
            overtime = OvertimeRecord(
                employee_id=employee.id,
                overtime_date=date(2026, 3, 12),
                overtime_type="weekday",
                hours=2,
                overtime_pay=0,
                use_comp_leave=True,
                comp_leave_granted=True,
                is_approved=True,
                approved_by="admin",
            )
            quota = LeaveQuota(
                employee_id=employee.id,
                year=2026,
                leave_type="compensatory",
                total_hours=2,
            )
            session.add_all([overtime, quota])
            overtime_id = overtime.id
            _create_user(
                session,
                username="admin_update_ot",
                password="AdminPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()
            overtime_id = overtime.id
            employee_id = employee.id

        login_res = _login(client, "admin_update_ot", "AdminPass123")
        assert login_res.status_code == 200

        fake_salary_engine.reset_mock()
        update_res = client.put(
            f"/api/overtimes/{overtime_id}",
            json={"hours": 1.5},
        )
        assert update_res.status_code == 200
        assert update_res.json()["salary_recalculated"] is True
        fake_salary_engine.process_salary_calculation.assert_called_once_with(employee_id, 2026, 3)

        with session_factory() as session:
            overtime = session.query(OvertimeRecord).filter(OvertimeRecord.id == overtime_id).one()
            quota = session.query(LeaveQuota).filter(
                LeaveQuota.employee_id == employee_id,
                LeaveQuota.year == 2026,
                LeaveQuota.leave_type == "compensatory",
            ).one()
            assert overtime.is_approved is None
            assert overtime.comp_leave_granted is False
            assert overtime.hours == 1.5
            assert quota.total_hours == 0.0

    def test_delete_approved_overtime_recalculates_salary(self, leave_overtime_client):
        client, session_factory, fake_salary_engine = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "E002", "員工乙")
            overtime = OvertimeRecord(
                employee_id=employee.id,
                overtime_date=date(2026, 4, 8),
                overtime_type="weekday",
                hours=2,
                overtime_pay=500,
                use_comp_leave=False,
                comp_leave_granted=False,
                is_approved=True,
                approved_by="admin",
            )
            session.add(overtime)
            _create_user(
                session,
                username="admin_delete_ot",
                password="AdminPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()
            overtime_id = overtime.id
            employee_id = employee.id

        login_res = _login(client, "admin_delete_ot", "AdminPass123")
        assert login_res.status_code == 200

        fake_salary_engine.reset_mock()
        delete_res = client.delete(f"/api/overtimes/{overtime_id}")
        assert delete_res.status_code == 200
        assert delete_res.json()["salary_recalculated"] is True
        fake_salary_engine.process_salary_calculation.assert_called_once_with(employee_id, 2026, 4)

        with session_factory() as session:
            overtime = session.query(OvertimeRecord).filter(OvertimeRecord.id == overtime_id).first()
            assert overtime is None

    def test_rejecting_previously_approved_comp_overtime_revokes_granted_quota(self, leave_overtime_client):
        client, session_factory, fake_salary_engine = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "E003", "員工丙")
            overtime = OvertimeRecord(
                employee_id=employee.id,
                overtime_date=date(2026, 5, 6),
                overtime_type="weekday",
                hours=3,
                overtime_pay=0,
                use_comp_leave=True,
                comp_leave_granted=True,
                is_approved=True,
                approved_by="admin",
            )
            quota = LeaveQuota(
                employee_id=employee.id,
                year=2026,
                leave_type="compensatory",
                total_hours=3,
            )
            session.add_all([overtime, quota])
            _create_user(
                session,
                username="admin_reject_ot",
                password="AdminPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()
            overtime_id = overtime.id
            employee_id = employee.id

        login_res = _login(client, "admin_reject_ot", "AdminPass123")
        assert login_res.status_code == 200

        fake_salary_engine.reset_mock()
        reject_res = client.put(
            f"/api/overtimes/{overtime_id}/approve",
            params={"approved": "false"},
        )
        assert reject_res.status_code == 200
        assert reject_res.json()["salary_recalculated"] is True
        fake_salary_engine.process_salary_calculation.assert_called_once_with(employee_id, 2026, 5)

        with session_factory() as session:
            overtime = session.query(OvertimeRecord).filter(OvertimeRecord.id == overtime_id).one()
            quota = session.query(LeaveQuota).filter(
                LeaveQuota.employee_id == employee_id,
                LeaveQuota.year == 2026,
                LeaveQuota.leave_type == "compensatory",
            ).one()
            assert overtime.is_approved is False
            assert overtime.approved_by is None
            assert overtime.comp_leave_granted is False
            assert quota.total_hours == 0.0
