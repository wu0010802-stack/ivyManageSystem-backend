"""Portal IDOR / 越權存取回歸測試。"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.portal.leaves import router as portal_leaves_router
from api.portal.schedule import router as portal_schedule_router
from models.database import Base, Employee, LeaveRecord, ShiftSwapRequest, User
from utils.auth import hash_password


@pytest.fixture
def portal_client(tmp_path):
    """建立隔離 sqlite 測試 app。"""
    db_path = tmp_path / "portal-idor-regressions.sqlite"
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
    app.include_router(portal_leaves_router, prefix="/api/portal")
    app.include_router(portal_schedule_router, prefix="/api/portal")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _create_employee(session, employee_id: str, name: str) -> Employee:
    employee = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=32000,
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
    employee: Employee,
) -> User:
    user = User(
        employee_id=employee.id,
        username=username,
        password_hash=hash_password(password),
        role="teacher",
        permissions=0,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


class TestPortalLeaveSubstituteRespondIdor:
    def test_unrelated_teacher_cannot_probe_other_leave_by_id(self, portal_client):
        client, session_factory = portal_client

        with session_factory() as session:
            requester = _create_employee(session, "T100", "請假老師")
            substitute = _create_employee(session, "T101", "代理老師")
            outsider = _create_employee(session, "T102", "路人老師")
            _create_user(session, username="requester", password="TempPass123", employee=requester)
            _create_user(session, username="substitute", password="TempPass123", employee=substitute)
            _create_user(session, username="outsider", password="TempPass123", employee=outsider)
            leave = LeaveRecord(
                employee_id=requester.id,
                leave_type="personal",
                start_date=date(2026, 3, 20),
                end_date=date(2026, 3, 20),
                leave_hours=8,
                substitute_employee_id=substitute.id,
                substitute_status="pending",
            )
            session.add(leave)
            session.commit()
            leave_id = leave.id

        login_res = _login(client, "outsider", "TempPass123")
        assert login_res.status_code == 200

        res = client.post(
            f"/api/portal/my-leaves/{leave_id}/substitute-respond",
            json={"action": "accept", "remark": "越權測試"},
        )

        assert res.status_code == 404


class TestPortalSwapRequestIdor:
    def test_unrelated_teacher_cannot_probe_other_swap_request_on_respond(self, portal_client):
        client, session_factory = portal_client

        with session_factory() as session:
            requester = _create_employee(session, "T200", "發起人")
            target = _create_employee(session, "T201", "換班對象")
            outsider = _create_employee(session, "T202", "路人老師")
            _create_user(session, username="swap_requester", password="TempPass123", employee=requester)
            _create_user(session, username="swap_target", password="TempPass123", employee=target)
            _create_user(session, username="swap_outsider", password="TempPass123", employee=outsider)
            swap = ShiftSwapRequest(
                requester_id=requester.id,
                target_id=target.id,
                swap_date=date(2026, 3, 21),
                status="pending",
            )
            session.add(swap)
            session.commit()
            swap_id = swap.id

        login_res = _login(client, "swap_outsider", "TempPass123")
        assert login_res.status_code == 200

        res = client.post(
            f"/api/portal/swap-requests/{swap_id}/respond",
            json={"action": "reject", "remark": "越權測試"},
        )

        assert res.status_code == 404

    def test_unrelated_teacher_cannot_probe_other_swap_request_on_cancel(self, portal_client):
        client, session_factory = portal_client

        with session_factory() as session:
            requester = _create_employee(session, "T210", "發起人")
            target = _create_employee(session, "T211", "換班對象")
            outsider = _create_employee(session, "T212", "路人老師")
            _create_user(session, username="cancel_requester", password="TempPass123", employee=requester)
            _create_user(session, username="cancel_target", password="TempPass123", employee=target)
            _create_user(session, username="cancel_outsider", password="TempPass123", employee=outsider)
            swap = ShiftSwapRequest(
                requester_id=requester.id,
                target_id=target.id,
                swap_date=date(2026, 3, 22),
                status="pending",
            )
            session.add(swap)
            session.commit()
            swap_id = swap.id

        login_res = _login(client, "cancel_outsider", "TempPass123")
        assert login_res.status_code == 200

        res = client.post(f"/api/portal/swap-requests/{swap_id}/cancel")

        assert res.status_code == 404
