"""sync_score_items 改造後行為驗收（從 4 條 auto → 14 條 auto），6 case。

複用 test_appraisal_score_preview.py 的 SQLite + real JWT login fixture pattern。
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
    ScoreItemCode,
    Semester,
)
from models.auth import User
from models.database import Base
from models.employee import Employee
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "appraisal-sync-extended.sqlite"
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


def test_sync_writes_14_item_codes_per_participant(client_with_db):
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_EVENT_WRITE)
        cycle, p = _seed_calibrate_fixtures(s)
        cycle_id, pid = cycle.id, p.id
    assert _login(client, "admin1").status_code == 200
    r = client.post(f"/api/appraisal/cycles/{cycle_id}/sync_score_items")
    assert r.status_code == 200, r.text
    with sf() as s:
        items = s.query(AppraisalScoreItem).filter_by(participant_id=pid).all()
        codes = {i.item_code for i in items}
    # SPED 由 apxlal01(2025-08-01) seed，非此 fixture 的 calibrate 規則；故排除
    assert codes == {c.value for c in ScoreItemCode if c.value != "SPED"}  # 14 條


def test_sync_preserves_manual_rows(client_with_db):
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_EVENT_WRITE)
        cycle, p = _seed_calibrate_fixtures(s)
        # 加 1 條人工 row（source_ref IS NULL）
        s.add(
            AppraisalScoreItem(
                cycle_id=cycle.id,
                participant_id=p.id,
                item_code="REWARD_PUNISH",
                sequence_no=2,  # 避開 sync 寫的 sequence_no=1，符合 UNIQUE 約束
                score_delta=Decimal("-7.5"),
                raw_value=Decimal("1"),
                source_ref=None,
                note="人工調整",
            )
        )
        s.commit()
        cycle_id, pid = cycle.id, p.id
    assert _login(client, "admin1").status_code == 200
    r = client.post(f"/api/appraisal/cycles/{cycle_id}/sync_score_items")
    assert r.status_code == 200
    with sf() as s:
        manual_rows = (
            s.query(AppraisalScoreItem)
            .filter_by(participant_id=pid, source_ref=None)
            .all()
        )
    assert len(manual_rows) == 1
    assert manual_rows[0].score_delta == Decimal("-7.5")
    assert manual_rows[0].note == "人工調整"


def test_sync_overwrites_auto_rows(client_with_db):
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_EVENT_WRITE)
        cycle, p = _seed_calibrate_fixtures(s)
        s.add(
            AppraisalScoreItem(
                cycle_id=cycle.id,
                participant_id=p.id,
                item_code="LATE_EARLY",
                score_delta=Decimal("-99"),
                raw_value=Decimal("0"),
                source_ref=f"auto:late_early:{cycle.id}",
            )
        )
        s.commit()
        cycle_id, pid = cycle.id, p.id
    assert _login(client, "admin1").status_code == 200
    client.post(f"/api/appraisal/cycles/{cycle_id}/sync_score_items")
    with sf() as s:
        row = (
            s.query(AppraisalScoreItem)
            .filter_by(participant_id=pid, item_code="LATE_EARLY")
            .first()
        )
    assert row is not None
    assert row.score_delta != Decimal("-99")  # 被新 sync 覆寫


def test_sync_dry_run_does_not_write(client_with_db):
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_EVENT_WRITE)
        cycle, _ = _seed_calibrate_fixtures(s)
        cycle_id = cycle.id
        before = s.query(AppraisalScoreItem).filter_by(cycle_id=cycle_id).count()
    assert _login(client, "admin1").status_code == 200
    r = client.post(f"/api/appraisal/cycles/{cycle_id}/sync_score_items?dry_run=true")
    assert r.status_code == 200
    with sf() as s:
        after = s.query(AppraisalScoreItem).filter_by(cycle_id=cycle_id).count()
    assert before == after
    body = r.json()
    assert "items" in body
    assert body["dry_run"] is True


def test_sync_blocked_when_cycle_locked(client_with_db):
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_EVENT_WRITE)
        cycle, _ = _seed_calibrate_fixtures(s, status=CycleStatus.LOCKED)
        cycle_id = cycle.id
    assert _login(client, "admin1").status_code == 200
    r = client.post(f"/api/appraisal/cycles/{cycle_id}/sync_score_items")
    assert r.status_code == 400


def test_sync_new_source_ref_format(client_with_db):
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_EVENT_WRITE)
        cycle, _ = _seed_calibrate_fixtures(s)
        cycle_id = cycle.id
    assert _login(client, "admin1").status_code == 200
    client.post(f"/api/appraisal/cycles/{cycle_id}/sync_score_items")
    with sf() as s:
        auto_rows = (
            s.query(AppraisalScoreItem)
            .filter(
                AppraisalScoreItem.cycle_id == cycle_id,
                AppraisalScoreItem.source_ref.like("auto:%"),
            )
            .all()
        )
    # SPED 由 apxlal01(2025-08-01) seed，非此 fixture 的 calibrate 規則；故排除
    expected_refs = {
        f"auto:{c.value.lower()}:{cycle_id}" for c in ScoreItemCode if c.value != "SPED"
    }
    actual_refs = {r.source_ref for r in auto_rows}
    assert actual_refs == expected_refs
