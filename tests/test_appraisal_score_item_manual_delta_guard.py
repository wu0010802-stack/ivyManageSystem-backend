"""add_score_item 對 MANUAL_DELTA code 的範圍守衛測試。

背景：manual_event_counts 路徑有 API 422 + 引擎 clamp 雙防線，但
POST /participants/{id}/score_items 原樣寫入 client 的 score_delta、
recompute 直接加總——同權限者可用此路徑灌超範圍分值繞過 MANUAL_DELTA 驗證。

守則：屬 MANUAL_DELTA 規則的 item_code 必須套同一範圍驗證（422）；
非 MANUAL_DELTA 的 free-form 人工列（如 REWARD_PUNISH 多筆手填）行為不變。
複用 test_appraisal_manual_event_counts.py 的 fixture pattern。
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


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "appraisal-score-item-guard.sqlite"
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


def _create_user(session, username, perms, password="TempPass123", role="admin"):
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


def _setup_cycle_with_participant(session):
    emp = Employee(employee_id="E001", name="王小華", is_active=True)
    session.add(emp)
    session.flush()
    cycle = AppraisalCycle(
        academic_year=114,
        semester=Semester.SECOND,
        start_date=date(2026, 2, 1),
        end_date=date(2026, 7, 31),
        base_score_calc_date=date(2026, 3, 15),
        base_score=Decimal("75.6"),
        status=CycleStatus.OPEN,
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
    return cycle.id, p.id


def _seed_supervisor_score_rule(session):
    """SUPERVISOR_SCORE：MANUAL_DELTA 0~10（對齊 aprreg01 seed）。"""
    session.add(
        AppraisalScoringRule(
            item_code="SUPERVISOR_SCORE",
            effective_from=date(2026, 2, 1),
            rule_type="MANUAL_DELTA",
            rule_config={"min_delta": "0", "max_delta": "10"},
        )
    )


class TestAddScoreItemManualDeltaGuard:
    def test_manual_delta_code_out_of_range_422(self, client_with_db):
        """MANUAL_DELTA code 超出規則範圍 → 422，且不得落 DB。"""
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", ["APPRAISAL_EVENT_WRITE"])
            cycle_id, p_id = _setup_cycle_with_participant(s)
            _seed_supervisor_score_rule(s)
            s.commit()
        assert _login(client, "admin1").status_code == 200

        r = client.post(
            f"/api/appraisal/participants/{p_id}/score_items",
            json={"item_code": "SUPERVISOR_SCORE", "score_delta": "9999"},
        )
        assert r.status_code == 422, r.text
        with sf() as s:
            assert (
                s.query(AppraisalScoreItem).filter_by(participant_id=p_id).count() == 0
            )

    def test_manual_delta_code_within_range_ok(self, client_with_db):
        """MANUAL_DELTA code 在範圍內 → 200 照常寫入。"""
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", ["APPRAISAL_EVENT_WRITE"])
            cycle_id, p_id = _setup_cycle_with_participant(s)
            _seed_supervisor_score_rule(s)
            s.commit()
        assert _login(client, "admin1").status_code == 200

        r = client.post(
            f"/api/appraisal/participants/{p_id}/score_items",
            json={"item_code": "SUPERVISOR_SCORE", "score_delta": "8"},
        )
        assert r.status_code == 200, r.text
        assert Decimal(str(r.json()["score_delta"])) == Decimal("8")

    def test_non_manual_delta_code_stays_free_form(self, client_with_db):
        """非 MANUAL_DELTA 規則的 code（free-form 人工列）不受範圍限制。"""
        client, sf = client_with_db
        with sf() as s:
            _create_user(s, "admin1", ["APPRAISAL_EVENT_WRITE"])
            cycle_id, p_id = _setup_cycle_with_participant(s)
            s.commit()
        assert _login(client, "admin1").status_code == 200

        r = client.post(
            f"/api/appraisal/participants/{p_id}/score_items",
            json={"item_code": "REWARD_PUNISH", "score_delta": "-12.5"},
        )
        assert r.status_code == 200, r.text
