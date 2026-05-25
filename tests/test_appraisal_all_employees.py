"""services/appraisal/status_aggregator.py 的 aggregate_all_active_employees_status
與 /all_employees_status、/participants:bulk_from_active endpoint 測試。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.appraisal import appraisal_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    CycleStatus,
    RoleGroup,
    Semester,
)
from models.auth import User
from models.classroom import Classroom
from models.database import Base
from models.employee import Employee, EmployeeType
from services.appraisal.status_aggregator import (
    aggregate_all_active_employees_status,
)
from utils.auth import hash_password
from utils.permissions import Permission

# ===== 共用 helpers =====


def _make_employee(
    session,
    name: str,
    eid: str = "E001",
    *,
    is_active: bool = True,
    supervisor_role: str | None = None,
    staff_role_category: str | None = None,
    classroom_id: int | None = None,
) -> Employee:
    emp = Employee(
        employee_id=eid,
        name=name,
        employee_type=EmployeeType.REGULAR.value,
        is_active=is_active,
        supervisor_role=supervisor_role,
        staff_role_category=staff_role_category,
        classroom_id=classroom_id,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_cycle(session, status: CycleStatus = CycleStatus.OPEN) -> AppraisalCycle:
    cycle = AppraisalCycle(
        academic_year=114,
        semester=Semester.FIRST,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
        base_score_calc_date=date(2025, 9, 15),
        base_score=Decimal("75.6"),
        status=status,
    )
    session.add(cycle)
    session.flush()
    return cycle


def _make_participant(
    session, cycle, emp, classroom_id=None, role_group=RoleGroup.HEAD_TEACHER
):
    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=emp.id,
        role_group=role_group,
        classroom_id=classroom_id,
        is_excluded=False,
    )
    session.add(p)
    session.flush()
    return p


# ===== Aggregator 單元測試（用 test_db_session fixture）=====


class TestAggregateAllActiveEmployees:
    def test_all_employees_includes_non_participants(self, test_db_session):
        s = test_db_session
        cycle = _make_cycle(s)
        emp_in = _make_employee(s, "已加入", eid="E001")
        emp_out_1 = _make_employee(s, "未加入A", eid="E002")
        emp_out_2 = _make_employee(s, "未加入B", eid="E003")
        _make_participant(s, cycle, emp_in)
        s.commit()

        out = aggregate_all_active_employees_status(s, cycle)
        assert len(out) == 3
        flags = {row.employee_id: row.is_participant for row in out}
        assert flags[emp_in.id] is True
        assert flags[emp_out_1.id] is False
        assert flags[emp_out_2.id] is False
        # 已加入者在前
        assert out[0].is_participant is True

    def test_all_employees_infers_role_group_from_employee(self, test_db_session):
        s = test_db_session
        cycle = _make_cycle(s)
        # supervisor_role 設值 → SUPERVISOR
        emp = _make_employee(s, "園長", eid="E010", supervisor_role="director")
        s.commit()

        out = aggregate_all_active_employees_status(s, cycle)
        assert len(out) == 1
        assert out[0].employee_id == emp.id
        assert out[0].is_participant is False
        assert out[0].role_group == RoleGroup.SUPERVISOR.value

    def test_all_employees_excludes_inactive_employees(self, test_db_session):
        s = test_db_session
        cycle = _make_cycle(s)
        _make_employee(s, "在職", eid="E020", is_active=True)
        _make_employee(s, "離職", eid="E021", is_active=False)
        s.commit()

        out = aggregate_all_active_employees_status(s, cycle)
        names = {r.employee_name for r in out}
        assert "在職" in names
        assert "離職" not in names


# ===== Endpoint 測試（用 TestClient fixture）=====


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "appraisal-all.sqlite"
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
    app.include_router(appraisal_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, username, perms, password="TempPass123") -> User:
    if isinstance(perms, str):
        perms = [perms]
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permission_names=perms,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password="TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


APPRAISAL_FULL = ["APPRAISAL_READ", "APPRAISAL_EVENT_WRITE", "APPRAISAL_REVIEW", "APPRAISAL_ACCOUNTING", "APPRAISAL_FINALIZE"]


class TestAllEmployeesStatusEndpoint:
    def test_get_all_employees_status_returns_all_active(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", APPRAISAL_FULL)
            cycle = _make_cycle(s)
            _make_employee(s, "A", eid="EA01")
            _make_employee(s, "B", eid="EB01")
            _make_employee(s, "C", eid="EC01", is_active=False)
            cycle_id = cycle.id
            s.commit()
        assert _login(client, "admin1").status_code == 200
        res = client.get(f"/api/appraisal/cycles/{cycle_id}/all_employees_status")
        assert res.status_code == 200, res.text
        data = res.json()
        # 只有 2 個 active employee（C 是 is_active=False）
        assert len(data["participants"]) == 2
        for row in data["participants"]:
            assert row["is_participant"] is False
            assert row["participant_id"] is None


class TestBulkAddFromActive:
    def test_bulk_add_creates_only_missing_employees(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", APPRAISAL_FULL)
            cycle = _make_cycle(s)
            emp_a = _make_employee(s, "A", eid="EA02")
            _make_employee(s, "B", eid="EB02")
            _make_employee(s, "C", eid="EC02")
            # A 已是 participant
            _make_participant(s, cycle, emp_a)
            cycle_id = cycle.id
            s.commit()
        assert _login(client, "admin1").status_code == 200
        res = client.post(
            f"/api/appraisal/cycles/{cycle_id}/participants:bulk_from_active",
            json={"employee_ids": None},
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["created_count"] == 2  # B, C
        assert data["skipped_count"] == 1  # A
        assert len(data["created_participants"]) == 2

    def test_bulk_add_rejected_on_locked_cycle(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", APPRAISAL_FULL)
            cycle = _make_cycle(s, status=CycleStatus.LOCKED)
            _make_employee(s, "A", eid="EA03")
            cycle_id = cycle.id
            s.commit()
        assert _login(client, "admin1").status_code == 200
        res = client.post(
            f"/api/appraisal/cycles/{cycle_id}/participants:bulk_from_active",
            json={"employee_ids": None},
        )
        assert res.status_code == 400, res.text
