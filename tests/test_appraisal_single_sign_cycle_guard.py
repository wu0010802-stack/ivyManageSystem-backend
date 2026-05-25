"""單筆 sign endpoints 對 cycle.status != OPEN 須擋（P1-3）。

對齊 batch_sign 既有行為（status code 400），避免封存週期被旁路偷簽。

fixture pattern 沿用 test_appraisal_reject_endpoint.py。
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
    CycleStatus,
    Grade,
    RoleGroup,
    Semester,
    SummaryStatus,
)
from models.auth import User
from models.database import Base
from models.employee import Employee, EmployeeType
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "appraisal-single-sign-guard.sqlite"
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
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text
    return res


def _seed_summary(s, summary_status, cycle_status=CycleStatus.OPEN):
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
        status=cycle_status,
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
        status=summary_status,
    )
    if summary_status in (
        SummaryStatus.SUPERVISOR_SIGNED,
        SummaryStatus.ACCOUNTING_SIGNED,
        SummaryStatus.FINALIZED,
    ):
        summary.supervisor_signed_by = 999
    if summary_status in (
        SummaryStatus.ACCOUNTING_SIGNED,
        SummaryStatus.FINALIZED,
    ):
        summary.accounting_signed_by = 999
    if summary_status == SummaryStatus.FINALIZED:
        summary.finalized_by = 999
    s.add(summary)
    s.commit()
    return summary, cycle


def test_sign_supervisor_blocked_when_cycle_locked(client_with_db):
    """cycle.status=LOCKED 時，單筆 sign_supervisor 也該擋（與 batch 一致 400）。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_REVIEW)
        summary, _ = _seed_summary(
            s, SummaryStatus.DRAFT, cycle_status=CycleStatus.LOCKED
        )
        sid = summary.id
    _login(client, "admin1")
    r = client.post(f"/api/appraisal/summaries/{sid}/sign_supervisor")
    assert r.status_code == 400, r.text
    assert (
        "LOCKED" in r.json().get("detail", "")
        or "封存" in r.json().get("detail", "")
        or "鎖" in r.json().get("detail", "")
    )


def test_sign_accounting_blocked_when_cycle_locked(client_with_db):
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_ACCOUNTING)
        summary, _ = _seed_summary(
            s, SummaryStatus.SUPERVISOR_SIGNED, cycle_status=CycleStatus.LOCKED
        )
        sid = summary.id
    _login(client, "admin1")
    r = client.post(f"/api/appraisal/summaries/{sid}/sign_accounting")
    assert r.status_code == 400, r.text


def test_finalize_blocked_when_cycle_locked(client_with_db):
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_FINALIZE)
        summary, _ = _seed_summary(
            s, SummaryStatus.ACCOUNTING_SIGNED, cycle_status=CycleStatus.LOCKED
        )
        sid = summary.id
    _login(client, "admin1")
    r = client.post(f"/api/appraisal/summaries/{sid}/finalize")
    assert r.status_code == 400, r.text


def test_recompute_blocked_when_cycle_locked(client_with_db):
    """recompute_summaries 也應守 cycle.status。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_EVENT_WRITE)
        _, cycle = _seed_summary(
            s, SummaryStatus.DRAFT, cycle_status=CycleStatus.LOCKED
        )
        cycle_id = cycle.id
    _login(client, "admin1")
    r = client.post(f"/api/appraisal/cycles/{cycle_id}/summaries:recompute")
    assert r.status_code == 400, r.text


def test_sign_supervisor_still_works_when_cycle_open(client_with_db):
    """sanity: cycle OPEN 時單筆 sign 正常通過。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_REVIEW)
        summary, _ = _seed_summary(
            s, SummaryStatus.DRAFT, cycle_status=CycleStatus.OPEN
        )
        sid = summary.id
    _login(client, "admin1")
    r = client.post(f"/api/appraisal/summaries/{sid}/sign_supervisor")
    assert r.status_code == 200, r.text
