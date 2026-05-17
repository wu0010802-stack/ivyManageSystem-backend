"""GET /summaries/{id}/logs 4 case 測試（Phase 2 Task 9）。

威脅：log 列出需 desc 排序、需 join users 取 actor_name；
空 list 要回 []；summary 不存在要 404。

fixture pattern 沿用 tests/test_appraisal_comment_endpoint.py。
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
    AppraisalSummary,
    AppraisalSummaryLog,
    CycleStatus,
    Grade,
    RoleGroup,
    Semester,
    SummaryLogAction,
    SummaryStatus,
)
from models.auth import User
from models.database import Base
from models.employee import Employee, EmployeeType
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "appraisal-summary-logs-endpoint.sqlite"
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


def _create_user(session, username, perms, password="TempPass123"):
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=int(perms),
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password="TempPass123"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text
    return res


def _seed_summary(s, status=SummaryStatus.DRAFT):
    emp = Employee(
        employee_id="E001",
        name="王老師",
        employee_type=EmployeeType.REGULAR.value,
        is_active=True,
    )
    s.add(emp)
    s.flush()
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
    s.flush()
    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=emp.id,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
        is_excluded=False,
    )
    s.add(p)
    s.flush()
    summary = AppraisalSummary(
        participant_id=p.id,
        cycle_id=cycle.id,
        base_score=Decimal("75.6"),
        event_score_sum=Decimal("0"),
        total_score=Decimal("75.6"),
        grade=Grade.PASS,
        bonus_amount=Decimal("0"),
        status=status,
    )
    s.add(summary)
    s.commit()
    return summary


# ===== 4 case =====


def test_get_logs_returns_two_comments(client_with_db):
    """寫兩條 COMMENT，list 出來兩條都在。"""
    client, sf = client_with_db
    with sf() as s:
        viewer = _create_user(s, "viewer1", Permission.APPRAISAL_READ)
        s.commit()
        actor_id = viewer.id
        summary = _seed_summary(s)
        summary_id = summary.id
        s.add_all(
            [
                AppraisalSummaryLog(
                    summary_id=summary_id,
                    action=SummaryLogAction.COMMENT,
                    actor_id=actor_id,
                    comment="first",
                ),
                AppraisalSummaryLog(
                    summary_id=summary_id,
                    action=SummaryLogAction.COMMENT,
                    actor_id=actor_id,
                    comment="second",
                ),
            ]
        )
        s.commit()
    _login(client, "viewer1")
    r = client.get(f"/api/appraisal/summaries/{summary_id}/logs")
    assert r.status_code == 200, r.text
    logs = r.json()
    assert len(logs) == 2
    comments = {l["comment"] for l in logs}
    assert comments == {"first", "second"}


def test_get_logs_includes_actor_name(client_with_db):
    """log 出來含 actor_name 欄位（join users）。"""
    client, sf = client_with_db
    with sf() as s:
        viewer = _create_user(s, "viewer2", Permission.APPRAISAL_READ)
        s.commit()
        actor_id = viewer.id
        summary = _seed_summary(s)
        summary_id = summary.id
        s.add(
            AppraisalSummaryLog(
                summary_id=summary_id,
                action=SummaryLogAction.SIGN_SUPERVISOR,
                actor_id=actor_id,
            )
        )
        s.commit()
    _login(client, "viewer2")
    r = client.get(f"/api/appraisal/summaries/{summary_id}/logs")
    assert r.status_code == 200
    logs = r.json()
    assert len(logs) == 1
    assert "actor_name" in logs[0]
    # viewer2 user 存在 → actor_name 應該是 "viewer2"（display_name 為 None 退 username）
    assert logs[0]["actor_name"] == "viewer2"


def test_get_logs_empty(client_with_db):
    """summary 存在但 0 log → []。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "viewer3", Permission.APPRAISAL_READ)
        s.commit()
        summary = _seed_summary(s)
        summary_id = summary.id
    _login(client, "viewer3")
    r = client.get(f"/api/appraisal/summaries/{summary_id}/logs")
    assert r.status_code == 200
    assert r.json() == []


def test_get_logs_404_summary_not_found(client_with_db):
    """summary 不存在 → 404。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "viewer4", Permission.APPRAISAL_READ)
        s.commit()
    _login(client, "viewer4")
    r = client.get("/api/appraisal/summaries/99999/logs")
    assert r.status_code == 404
