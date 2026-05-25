"""3 既有 sign endpoint 改造後寫 log 驗證（Phase 2 Task 5）。

威脅：原 sign_supervisor / sign_accounting / finalize_summary 三函式只更新
summary 狀態與 signed_by/at 欄位，沒有寫入 AppraisalSummaryLog 軌跡。
本檔驗證三函式各 commit 時會 INSERT 一條對應 action 的 log。

fixture pattern 沿用 test_appraisal_scoring_rules_endpoint.py（SQLite + 真實
JWT cookie login）。auth_header 概念以 cookie 形式落實。
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
    db_path = tmp_path / "appraisal-existing-sign-logs.sqlite"
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
    """admin 角色、無 employee_id（避免 assert_not_self_approval 誤殺）。"""
    # 單一 Permission/str → wrap 成 list
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


def _seed_draft_summary(s):
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
        status=SummaryStatus.DRAFT,
    )
    s.add(summary)
    s.commit()
    return cycle, p, summary


def test_sign_supervisor_writes_log(client_with_db):
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "supervisor1", Permission.APPRAISAL_REVIEW)
        s.commit()
        _, _, summary = _seed_draft_summary(s)
        summary_id = summary.id
    _login(client, "supervisor1")
    r = client.post(f"/api/appraisal/summaries/{summary_id}/sign_supervisor?comment=ok")
    assert r.status_code == 200, r.text
    with sf() as s:
        logs = s.query(AppraisalSummaryLog).filter_by(summary_id=summary_id).all()
        assert len(logs) == 1
        assert logs[0].action == SummaryLogAction.SIGN_SUPERVISOR
        assert logs[0].from_status == SummaryStatus.DRAFT
        assert logs[0].to_status == SummaryStatus.SUPERVISOR_SIGNED
        assert logs[0].comment == "ok"


def test_sign_accounting_writes_log(client_with_db):
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "accountant1", Permission.APPRAISAL_ACCOUNTING)
        s.commit()
        _, _, summary = _seed_draft_summary(s)
        summary.status = SummaryStatus.SUPERVISOR_SIGNED
        s.commit()
        summary_id = summary.id
    _login(client, "accountant1")
    r = client.post(f"/api/appraisal/summaries/{summary_id}/sign_accounting")
    assert r.status_code == 200, r.text
    with sf() as s:
        logs = s.query(AppraisalSummaryLog).filter_by(summary_id=summary_id).all()
        assert len(logs) == 1
        assert logs[0].action == SummaryLogAction.SIGN_ACCOUNTING
        assert logs[0].from_status == SummaryStatus.SUPERVISOR_SIGNED
        assert logs[0].to_status == SummaryStatus.ACCOUNTING_SIGNED


def test_finalize_writes_log(client_with_db):
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "finalizer1", Permission.APPRAISAL_FINALIZE)
        s.commit()
        _, _, summary = _seed_draft_summary(s)
        summary.status = SummaryStatus.ACCOUNTING_SIGNED
        s.commit()
        summary_id = summary.id
    _login(client, "finalizer1")
    r = client.post(f"/api/appraisal/summaries/{summary_id}/finalize")
    assert r.status_code == 200, r.text
    with sf() as s:
        logs = s.query(AppraisalSummaryLog).filter_by(summary_id=summary_id).all()
        assert len(logs) == 1
        assert logs[0].action == SummaryLogAction.FINALIZE
        assert logs[0].from_status == SummaryStatus.ACCOUNTING_SIGNED
        assert logs[0].to_status == SummaryStatus.FINALIZED
