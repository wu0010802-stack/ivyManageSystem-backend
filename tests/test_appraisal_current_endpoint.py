"""api/appraisal 新 endpoint 測試（/current /by_year /aggregated_status /sync_score_items）。

複用 academic_summary_endpoint 的 TestClient + SQLite fixture pattern。
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
    AppraisalScoreItem,
    CycleStatus,
    RoleGroup,
    Semester,
)
from models.database import Base
from models.auth import User
from models.classroom import LIFECYCLE_ACTIVE, Classroom, Student
from models.employee import Employee, EmployeeType
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "appraisal-current.sqlite"
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


def _create_user(
    session, username, perms, password="TempPass123", role="admin"
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=int(perms),
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password="TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _make_employee(session, name, eid="E001") -> Employee:
    emp = Employee(
        employee_id=eid,
        name=name,
        employee_type=EmployeeType.REGULAR.value,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_cycle(
    session,
    sy=114,
    sem=Semester.FIRST,
    status=CycleStatus.OPEN,
) -> AppraisalCycle:
    cycle = AppraisalCycle(
        academic_year=sy,
        semester=sem,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
        base_score_calc_date=date(2025, 9, 15),
        base_score=Decimal("75.6"),
        status=status,
    )
    session.add(cycle)
    session.flush()
    return cycle


def _make_participant(session, cycle, emp, classroom_id=None) -> AppraisalParticipant:
    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=emp.id,
        role_group=RoleGroup.HEAD_TEACHER,
        classroom_id=classroom_id,
        is_excluded=False,
    )
    session.add(p)
    session.flush()
    return p


# 完整考核權限位元（READ + WRITE + REVIEW + ACCOUNTING + FINALIZE）
APPRAISAL_FULL = (
    Permission.APPRAISAL_READ
    | Permission.APPRAISAL_EVENT_WRITE
    | Permission.APPRAISAL_REVIEW
    | Permission.APPRAISAL_ACCOUNTING
    | Permission.APPRAISAL_FINALIZE
)


class TestGetCurrent:
    def test_get_current_returns_existing_cycle(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", APPRAISAL_FULL)
            _make_cycle(s, sy=114, sem=Semester.FIRST)
            s.commit()
        assert _login(client, "admin1").status_code == 200
        res = client.get(
            "/api/appraisal/current", params={"school_year": 114, "semester": 1}
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data is not None
        assert data["academic_year"] == 114
        assert data["semester"] == "FIRST"

    def test_get_current_returns_null_when_no_cycle(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", APPRAISAL_FULL)
            s.commit()
        assert _login(client, "admin1").status_code == 200
        res = client.get(
            "/api/appraisal/current", params={"school_year": 200, "semester": 2}
        )
        assert res.status_code == 200, res.text
        assert res.json() is None


class TestByYear:
    def test_get_by_year_returns_first_and_second(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", APPRAISAL_FULL)
            _make_cycle(s, sy=114, sem=Semester.FIRST)
            # second semester（同一學年）
            s.add(
                AppraisalCycle(
                    academic_year=114,
                    semester=Semester.SECOND,
                    start_date=date(2026, 2, 1),
                    end_date=date(2026, 7, 31),
                    base_score_calc_date=date(2026, 3, 15),
                    base_score=Decimal("80.0"),
                    status=CycleStatus.OPEN,
                )
            )
            s.commit()
        assert _login(client, "admin1").status_code == 200
        res = client.get("/api/appraisal/by_year/114")
        assert res.status_code == 200, res.text
        data = res.json()
        assert len(data) == 2
        # 排序：FIRST 在前
        assert data[0]["semester"] == "FIRST"
        assert data[1]["semester"] == "SECOND"


class TestAggregatedStatus:
    def test_get_aggregated_status_unauthorized_without_permission(
        self, client_with_db
    ):
        client, sf = client_with_db
        with sf() as s:
            # 沒有 APPRAISAL_READ
            _create_user(s, "noperm", Permission.CLASSROOMS_READ)
            cycle = _make_cycle(s)
            cycle_id = cycle.id
            s.commit()
        assert _login(client, "noperm").status_code == 200
        res = client.get(f"/api/appraisal/cycles/{cycle_id}/aggregated_status")
        assert res.status_code == 403

    def test_get_aggregated_status_returns_all_participants_with_four_aggregates(
        self, client_with_db
    ):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", APPRAISAL_FULL)
            cycle = _make_cycle(s)
            cls = Classroom(name="A", school_year=114, semester=1, is_active=True)
            s.add(cls)
            s.flush()
            emp_a = _make_employee(s, "A", eid="EA01")
            emp_b = _make_employee(s, "B", eid="EB01")
            _make_participant(s, cycle, emp_a, classroom_id=cls.id)
            _make_participant(s, cycle, emp_b, classroom_id=None)
            cycle_id = cycle.id
            s.commit()
        assert _login(client, "admin1").status_code == 200
        res = client.get(f"/api/appraisal/cycles/{cycle_id}/aggregated_status")
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["cycle_id"] == cycle_id
        assert data["academic_year"] == 114
        assert data["semester"] == "FIRST"
        assert len(data["participants"]) == 2
        for p in data["participants"]:
            assert "attendance" in p
            assert "retention" in p
            assert "activity" in p
            assert "disciplinary" in p


class TestSyncScoreItems:
    def test_sync_score_items_dry_run_does_not_write(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", APPRAISAL_FULL)
            cycle = _make_cycle(s)
            emp = _make_employee(s, "A")
            _make_participant(s, cycle, emp)
            cycle_id = cycle.id
            s.commit()
        assert _login(client, "admin1").status_code == 200
        res = client.post(
            f"/api/appraisal/cycles/{cycle_id}/sync_score_items",
            params={"dry_run": "true"},
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["dry_run"] is True
        assert data["inserted_count"] == 4  # 1 participant × 4 items
        # DB 不應有 row
        with sf() as s:
            assert s.query(AppraisalScoreItem).count() == 0

    def test_sync_score_items_replaces_only_auto_rows(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", APPRAISAL_FULL)
            cycle = _make_cycle(s)
            emp = _make_employee(s, "A")
            p = _make_participant(s, cycle, emp)
            cycle_id = cycle.id
            participant_id = p.id
            # 1 個人工 row（source_ref IS NULL）
            s.add(
                AppraisalScoreItem(
                    participant_id=participant_id,
                    cycle_id=cycle_id,
                    item_code="LATE_EARLY",
                    sequence_no=99,
                    score_delta=Decimal("-5.0"),
                    note="人工手動扣",
                    source_ref=None,
                )
            )
            s.commit()
        assert _login(client, "admin1").status_code == 200
        # 第一次同步
        res = client.post(f"/api/appraisal/cycles/{cycle_id}/sync_score_items")
        assert res.status_code == 200, res.text
        d1 = res.json()
        assert d1["dry_run"] is False
        assert d1["deleted_count"] == 0
        assert d1["inserted_count"] == 4
        assert d1["skipped_manual_count"] == 1
        with sf() as s:
            rows = s.query(AppraisalScoreItem).filter_by(cycle_id=cycle_id).all()
            assert len(rows) == 5  # 4 auto + 1 manual
            manual = [r for r in rows if r.source_ref is None]
            assert len(manual) == 1
            assert manual[0].score_delta == Decimal("-5.0")
        # 第二次同步：舊 4 auto 被刪、新 4 auto 寫入；人工仍在
        res = client.post(f"/api/appraisal/cycles/{cycle_id}/sync_score_items")
        assert res.status_code == 200, res.text
        d2 = res.json()
        assert d2["deleted_count"] == 4
        assert d2["inserted_count"] == 4
        assert d2["skipped_manual_count"] == 1
        with sf() as s:
            rows = s.query(AppraisalScoreItem).filter_by(cycle_id=cycle_id).all()
            assert len(rows) == 5
            manual = [r for r in rows if r.source_ref is None]
            assert len(manual) == 1
            assert manual[0].note == "人工手動扣"

    def test_sync_score_items_rejected_on_locked_cycle(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", APPRAISAL_FULL)
            cycle = _make_cycle(s, status=CycleStatus.LOCKED)
            emp = _make_employee(s, "A")
            _make_participant(s, cycle, emp)
            cycle_id = cycle.id
            s.commit()
        assert _login(client, "admin1").status_code == 200
        res = client.post(f"/api/appraisal/cycles/{cycle_id}/sync_score_items")
        assert res.status_code == 400
        assert "LOCKED" in res.text
