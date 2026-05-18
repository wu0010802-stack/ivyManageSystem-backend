"""POST /summaries/{id}/comment 3 case 測試（Phase 2 Task 7）。

威脅：留言 endpoint 必須寫 AppraisalSummaryLog action=COMMENT，但 status 不變；
空 comment 必須擋（Pydantic min_length=1 → 422）；完全沒考核權限 → 403
（endpoint 入口最低門檻是 APPRAISAL_READ）。

fixture pattern 沿用 tests/test_appraisal_reject_endpoint.py（SQLite + 真實 JWT
cookie login）。
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
    db_path = tmp_path / "appraisal-comment-endpoint.sqlite"
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


def _seed_summary(s, status=SummaryStatus.SUPERVISOR_SIGNED):
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
    if status in (
        SummaryStatus.SUPERVISOR_SIGNED,
        SummaryStatus.ACCOUNTING_SIGNED,
        SummaryStatus.FINALIZED,
    ):
        summary.supervisor_signed_by = 999
    if status in (SummaryStatus.ACCOUNTING_SIGNED, SummaryStatus.FINALIZED):
        summary.accounting_signed_by = 999
    if status == SummaryStatus.FINALIZED:
        summary.finalized_by = 999
    s.add(summary)
    s.commit()
    return summary


# ===== 3 case =====


def test_comment_writes_log_no_status_change(client_with_db):
    """寫 log action=COMMENT，status 不變。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "commenter1", Permission.APPRAISAL_READ)
        s.commit()
        summary = _seed_summary(s, SummaryStatus.SUPERVISOR_SIGNED)
        summary_id = summary.id
    _login(client, "commenter1")
    r = client.post(
        f"/api/appraisal/summaries/{summary_id}/comment",
        json={"comment": "ok"},
    )
    assert r.status_code == 200, r.text
    with sf() as s:
        fresh = s.get(AppraisalSummary, summary_id)
        assert fresh.status == SummaryStatus.SUPERVISOR_SIGNED  # 不變
        logs = s.query(AppraisalSummaryLog).filter_by(summary_id=summary_id).all()
        assert len(logs) == 1
        assert logs[0].action == SummaryLogAction.COMMENT
        assert logs[0].comment == "ok"


def test_comment_empty_rejected(client_with_db):
    """空 comment → 422（Pydantic min_length=1 攔截）。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "commenter2", Permission.APPRAISAL_READ)
        s.commit()
        summary = _seed_summary(s, SummaryStatus.SUPERVISOR_SIGNED)
        summary_id = summary.id
    _login(client, "commenter2")
    r = client.post(
        f"/api/appraisal/summaries/{summary_id}/comment",
        json={"comment": ""},
    )
    assert r.status_code == 422


def test_comment_requires_read_permission(client_with_db):
    """完全沒考核權限 → 403（endpoint 入口最低門檻是 APPRAISAL_READ）。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "noperm", Permission(0))
        s.commit()
        summary = _seed_summary(s, SummaryStatus.SUPERVISOR_SIGNED)
        summary_id = summary.id
    _login(client, "noperm")
    r = client.post(
        f"/api/appraisal/summaries/{summary_id}/comment",
        json={"comment": "x"},
    )
    assert r.status_code == 403


def test_comment_blocks_self_approval(client_with_db):
    """bug sweep 2026-05-18 P2：本人不可對自己的 summary 留言（403）。"""
    client, sf = client_with_db
    with sf() as s:
        summary = _seed_summary(s, SummaryStatus.SUPERVISOR_SIGNED)
        summary_id = summary.id
        emp_id = summary.participant.employee_id
        # 建一個與 summary 同 employee_id 的 user（教師本人）
        user = User(
            username="self_user",
            password_hash=hash_password("TempPass123"),
            role="teacher",
            permissions=int(Permission.APPRAISAL_READ),
            is_active=True,
            employee_id=emp_id,
        )
        s.add(user)
        s.commit()
    _login(client, "self_user")
    r = client.post(
        f"/api/appraisal/summaries/{summary_id}/comment",
        json={"comment": "self"},
    )
    assert r.status_code == 403, r.text
    assert "自行簽核" in r.json().get("detail", "")
