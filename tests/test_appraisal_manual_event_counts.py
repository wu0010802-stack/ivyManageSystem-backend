"""appraisal manual_event_counts endpoint 測試（6 case）。

複用 test_appraisal_scoring_rules_endpoint.py 的 SQLite + login 真實 JWT fixture pattern。
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
    AppraisalManualEventCount,
    AppraisalParticipant,
    CycleStatus,
    RoleGroup,
    Semester,
)
from models.auth import User
from models.database import Base
from models.employee import Employee
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "appraisal-manual-event-counts.sqlite"
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


def _setup_cycle_with_participant(session, status=CycleStatus.OPEN):
    """建立一個 cycle + 一個 employee + 一個 participant，回傳 (cycle_id, participant_id, employee_name)。"""
    emp = Employee(
        employee_id="E001",
        name="王小華",
        is_active=True,
    )
    session.add(emp)
    session.flush()

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

    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=emp.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
        is_excluded=False,
    )
    session.add(p)
    session.flush()
    return cycle.id, p.id, emp.name


# === GET /cycles/{cycle_id}/manual_event_counts ===


class TestListManualEventCounts:
    def test_get_empty_returns_empty_list(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", Permission.APPRAISAL_READ)
            cycle_id, _, _ = _setup_cycle_with_participant(s)
            s.commit()
        assert _login(client, "admin1").status_code == 200
        r = client.get(f"/api/appraisal/cycles/{cycle_id}/manual_event_counts")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["cycle_id"] == cycle_id
        assert body["entries"] == []

    def test_get_returns_existing_counts_with_employee_name(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", Permission.APPRAISAL_READ)
            cycle_id, p_id, emp_name = _setup_cycle_with_participant(s)
            s.add(
                AppraisalManualEventCount(
                    cycle_id=cycle_id,
                    participant_id=p_id,
                    item_code="SCHOOL_MEETING_ABSENCE",
                    count=Decimal("2"),
                )
            )
            s.commit()
        assert _login(client, "admin1").status_code == 200
        r = client.get(f"/api/appraisal/cycles/{cycle_id}/manual_event_counts")
        assert r.status_code == 200, r.text
        entries = r.json()["entries"]
        assert len(entries) == 1
        assert entries[0]["item_code"] == "SCHOOL_MEETING_ABSENCE"
        assert Decimal(str(entries[0]["count"])) == Decimal("2")
        assert entries[0]["employee_name"] == emp_name


# === PUT /cycles/{cycle_id}/manual_event_counts:batch ===


class TestBatchUpsertManualEventCounts:
    def test_batch_upsert_inserts_and_updates(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(
                s,
                "admin1",
                Permission.APPRAISAL_EVENT_WRITE | Permission.APPRAISAL_READ,
            )
            cycle_id, p_id, _ = _setup_cycle_with_participant(s)
            s.commit()
        assert _login(client, "admin1").status_code == 200

        # 第一次 INSERT 兩筆
        r = client.put(
            f"/api/appraisal/cycles/{cycle_id}/manual_event_counts:batch",
            json={
                "entries": [
                    {
                        "participant_id": p_id,
                        "item_code": "SCHOOL_MEETING_ABSENCE",
                        "count": "2",
                    },
                    {
                        "participant_id": p_id,
                        "item_code": "CHILD_ACCIDENT",
                        "count": "1",
                    },
                ]
            },
        )
        assert r.status_code == 200, r.text
        with sf() as s:
            assert (
                s.query(AppraisalManualEventCount).filter_by(cycle_id=cycle_id).count()
                == 2
            )

        # 第二次 UPSERT（同 key 改值）
        r = client.put(
            f"/api/appraisal/cycles/{cycle_id}/manual_event_counts:batch",
            json={
                "entries": [
                    {
                        "participant_id": p_id,
                        "item_code": "SCHOOL_MEETING_ABSENCE",
                        "count": "5",
                    }
                ]
            },
        )
        assert r.status_code == 200, r.text
        with sf() as s:
            row = (
                s.query(AppraisalManualEventCount)
                .filter_by(
                    cycle_id=cycle_id,
                    participant_id=p_id,
                    item_code="SCHOOL_MEETING_ABSENCE",
                )
                .first()
            )
            assert row.count == Decimal("5")
            # CHILD_ACCIDENT 仍存在（不會被刪）
            assert (
                s.query(AppraisalManualEventCount).filter_by(cycle_id=cycle_id).count()
                == 2
            )

    def test_batch_upsert_rejects_negative_count(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(
                s,
                "admin1",
                Permission.APPRAISAL_EVENT_WRITE | Permission.APPRAISAL_READ,
            )
            cycle_id, p_id, _ = _setup_cycle_with_participant(s)
            s.commit()
        assert _login(client, "admin1").status_code == 200
        r = client.put(
            f"/api/appraisal/cycles/{cycle_id}/manual_event_counts:batch",
            json={
                "entries": [
                    {
                        "participant_id": p_id,
                        "item_code": "OTHER",
                        "count": "-1",
                    }
                ]
            },
        )
        assert r.status_code == 422, r.text

    def test_batch_upsert_blocked_when_cycle_locked(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            _create_user(
                s,
                "admin1",
                Permission.APPRAISAL_EVENT_WRITE | Permission.APPRAISAL_READ,
            )
            cycle_id, p_id, _ = _setup_cycle_with_participant(
                s, status=CycleStatus.LOCKED
            )
            s.commit()
        assert _login(client, "admin1").status_code == 200
        r = client.put(
            f"/api/appraisal/cycles/{cycle_id}/manual_event_counts:batch",
            json={
                "entries": [
                    {
                        "participant_id": p_id,
                        "item_code": "OTHER",
                        "count": "1",
                    }
                ]
            },
        )
        assert r.status_code == 400, r.text

    def test_batch_upsert_requires_event_write_perm(self, client_with_db):
        client, sf = client_with_db
        with sf() as s:
            # 只有 APPRAISAL_READ，沒 APPRAISAL_EVENT_WRITE
            _create_user(s, "readonly", Permission.APPRAISAL_READ)
            cycle_id, p_id, _ = _setup_cycle_with_participant(s)
            s.commit()
        assert _login(client, "readonly").status_code == 200
        r = client.put(
            f"/api/appraisal/cycles/{cycle_id}/manual_event_counts:batch",
            json={
                "entries": [
                    {
                        "participant_id": p_id,
                        "item_code": "OTHER",
                        "count": "1",
                    }
                ]
            },
        )
        assert r.status_code == 403, r.text
