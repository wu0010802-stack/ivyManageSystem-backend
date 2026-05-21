"""score_preview endpoint 測試（4 case）。

複用 test_appraisal_manual_event_counts.py 的 SQLite + login 真實 JWT fixture pattern。
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
    AppraisalScoringRule,
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
    db_path = tmp_path / "appraisal-score-preview.sqlite"
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
    if isinstance(perms, str):
        perms = [perms]
    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
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


def _seed_calibrate_fixtures(session, status=CycleStatus.OPEN):
    """建 cycle + participant(with Employee) + 14 default rules，回傳 (cycle, participant)。"""
    emp = Employee(employee_id="E001", name="王小華", is_active=True)
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
    defaults = [
        ("LATE_EARLY", "PER_UNIT", {"per_unit_delta": -0.25}),
        ("MISSING_PUNCH", "PER_UNIT", {"per_unit_delta": -0.25}),
        ("LEAVE", "PER_UNIT", {"per_unit_delta": -1.0}),
        (
            "RETURNING_RATE_0915",
            "TIER",
            {"input_field": "retention_rate", "tiers": [{"min": 0, "delta": 0}]},
        ),
        (
            "RETURNING_RATE_0315",
            "TIER",
            {
                "input_field": "retention_rate",
                "tiers": [
                    {"min": 100, "delta": 6},
                    {"min": 0, "delta": -6},
                ],
            },
        ),
        (
            "AFTER_CLASS_RATE",
            "FLAT_THRESHOLD",
            {
                "input_field": "activity_rate",
                "threshold": 80,
                "above_delta": 2,
                "below_delta": 0,
            },
        ),
        (
            "REWARD_PUNISH",
            "DISCIPLINARY_TIERED",
            {"warning_delta": -1, "minor_delta": -3, "major_delta": -10},
        ),
        ("SCHOOL_MEETING_ABSENCE", "PER_UNIT", {"per_unit_delta": -1}),
        ("INSTITUTION_MEETING_0913", "PER_UNIT", {"per_unit_delta": -2}),
        ("INSTITUTION_MEETING_1115", "PER_UNIT", {"per_unit_delta": -2}),
        ("SELF_IMPROVEMENT_ACTIVITY", "PER_UNIT", {"per_unit_delta": -2}),
        ("CHILD_ACCIDENT", "PER_UNIT", {"per_unit_delta": -3}),
        ("CLASS_HEADCOUNT_BONUS", "PER_UNIT", {"per_unit_delta": 2}),
        ("OTHER", "PER_UNIT", {"per_unit_delta": 0}),
    ]
    for code, rt, cfg in defaults:
        session.add(
            AppraisalScoringRule(
                item_code=code,
                effective_from=date(2025, 1, 1),
                rule_type=rt,
                rule_config=cfg,
            )
        )
    session.commit()
    return cycle, p


def test_preview_returns_all_14_items_per_participant(client_with_db):
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_READ)
        cycle, _p = _seed_calibrate_fixtures(s)
        cycle_id = cycle.id
    assert _login(client, "admin1").status_code == 200
    r = client.post(f"/api/appraisal/cycles/{cycle_id}/score_preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["participants"]) >= 1
    items = body["participants"][0]["items"]
    item_codes = {i["item_code"] for i in items}
    assert len(item_codes) == 14


def test_preview_highlights_diff_from_db(client_with_db):
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_READ)
        cycle, p = _seed_calibrate_fixtures(s)
        s.add(
            AppraisalScoreItem(
                cycle_id=cycle.id,
                participant_id=p.id,
                item_code="LATE_EARLY",
                score_delta=Decimal("-99"),
                raw_value=Decimal("0"),
            )
        )
        s.commit()
        cycle_id = cycle.id
    assert _login(client, "admin1").status_code == 200
    r = client.post(f"/api/appraisal/cycles/{cycle_id}/score_preview")
    assert r.status_code == 200, r.text
    body = r.json()
    late = next(
        i for i in body["participants"][0]["items"] if i["item_code"] == "LATE_EARLY"
    )
    assert Decimal(str(late["current_db_value"])) == Decimal("-99")
    assert Decimal(str(late["delta"])) != Decimal("-99")


def test_preview_without_rules_returns_empty_items(client_with_db):
    """rules 表空 → preview 應 graceful（200）。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_READ)
        cycle = AppraisalCycle(
            academic_year=114,
            semester=Semester.FIRST,
            start_date=date(2025, 8, 1),
            end_date=date(2026, 1, 31),
            base_score_calc_date=date(2025, 9, 15),
            base_score=Decimal("75.6"),
            status=CycleStatus.OPEN,
        )
        s.add(cycle)
        s.commit()
        cycle_id = cycle.id
    assert _login(client, "admin1").status_code == 200
    r = client.post(f"/api/appraisal/cycles/{cycle_id}/score_preview")
    assert r.status_code == 200, r.text


def test_preview_404_for_unknown_cycle(client_with_db):
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_READ)
        s.commit()
    assert _login(client, "admin1").status_code == 200
    r = client.post("/api/appraisal/cycles/99999/score_preview")
    assert r.status_code == 404
