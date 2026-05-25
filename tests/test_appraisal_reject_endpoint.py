"""POST /summaries/{id}/reject 8 case 測試（Phase 2 Task 6）。

威脅：退簽必須能從各階段（SUPERVISOR_SIGNED / ACCOUNTING_SIGNED / FINALIZED）
按預設退一階或顯式指定 to_status；同時必須寫 AppraisalSummaryLog、清對應
signed_by 欄位、留下 rejected_reason；DRAFT 不可退；reason 不足/權限不足要擋。

fixture pattern 沿用 test_appraisal_existing_sign_logs.py（SQLite + 真實 JWT
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
    db_path = tmp_path / "appraisal-reject-endpoint.sqlite"
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
    # 依當前 status 填上各階 signed_by（用一個假 user_id=999；不會與測試
    # 自己建的 actor 重疊衝撞）
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


# ===== 8 case =====


def test_reject_supervisor_to_draft(client_with_db):
    """SUPERVISOR_SIGNED + APPRAISAL_REVIEW + 預設 to_status → DRAFT。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "reviewer1", ["APPRAISAL_READ", "APPRAISAL_REVIEW"])
        s.commit()
        summary = _seed_summary(s, SummaryStatus.SUPERVISOR_SIGNED)
        summary_id = summary.id
    _login(client, "reviewer1")
    r = client.post(
        f"/api/appraisal/summaries/{summary_id}/reject",
        json={"reason": "工時計算錯誤，請重核"},
    )
    assert r.status_code == 200, r.text
    with sf() as s:
        fresh = s.get(AppraisalSummary, summary_id)
        assert fresh.status == SummaryStatus.DRAFT
        assert fresh.supervisor_signed_by is None
        assert fresh.rejected_reason == "工時計算錯誤，請重核"
        logs = s.query(AppraisalSummaryLog).filter_by(summary_id=summary_id).all()
        assert len(logs) == 1
        assert logs[0].action == SummaryLogAction.REJECT
        assert logs[0].from_status == SummaryStatus.SUPERVISOR_SIGNED
        assert logs[0].to_status == SummaryStatus.DRAFT


def test_reject_accounting_default_to_supervisor(client_with_db):
    """ACCOUNTING_SIGNED + APPRAISAL_ACCOUNTING + 預設 → SUPERVISOR_SIGNED。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(
            s,
            "accountant1",
            ["APPRAISAL_READ", "APPRAISAL_ACCOUNTING"],
        )
        s.commit()
        summary = _seed_summary(s, SummaryStatus.ACCOUNTING_SIGNED)
        summary_id = summary.id
    _login(client, "accountant1")
    r = client.post(
        f"/api/appraisal/summaries/{summary_id}/reject",
        json={"reason": "金額異常，請主管重簽"},
    )
    assert r.status_code == 200, r.text
    with sf() as s:
        fresh = s.get(AppraisalSummary, summary_id)
        assert fresh.status == SummaryStatus.SUPERVISOR_SIGNED
        assert fresh.accounting_signed_by is None
        # supervisor 階保留
        assert fresh.supervisor_signed_by == 999


def test_reject_accounting_to_draft_explicit(client_with_db):
    """ACCOUNTING_SIGNED + APPRAISAL_ACCOUNTING + 顯式 to_status=DRAFT。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(
            s,
            "accountant2",
            ["APPRAISAL_READ", "APPRAISAL_ACCOUNTING"],
        )
        s.commit()
        summary = _seed_summary(s, SummaryStatus.ACCOUNTING_SIGNED)
        summary_id = summary.id
    _login(client, "accountant2")
    r = client.post(
        f"/api/appraisal/summaries/{summary_id}/reject",
        json={"reason": "需重新計算 base", "to_status": "DRAFT"},
    )
    assert r.status_code == 200, r.text
    with sf() as s:
        fresh = s.get(AppraisalSummary, summary_id)
        assert fresh.status == SummaryStatus.DRAFT
        assert fresh.supervisor_signed_by is None
        assert fresh.accounting_signed_by is None


def test_reject_finalized_to_accounting(client_with_db):
    """FINALIZED + APPRAISAL_FINALIZE + 預設 → ACCOUNTING_SIGNED。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(
            s,
            "finalizer1",
            ["APPRAISAL_READ", "APPRAISAL_FINALIZE"],
        )
        s.commit()
        summary = _seed_summary(s, SummaryStatus.FINALIZED)
        summary_id = summary.id
    _login(client, "finalizer1")
    r = client.post(
        f"/api/appraisal/summaries/{summary_id}/reject",
        json={"reason": "稅務調整需重新核定獎金，請會計重核"},
    )
    assert r.status_code == 200, r.text
    with sf() as s:
        fresh = s.get(AppraisalSummary, summary_id)
        assert fresh.status == SummaryStatus.ACCOUNTING_SIGNED
        assert fresh.finalized_by is None
        # 前兩階保留
        assert fresh.supervisor_signed_by == 999
        assert fresh.accounting_signed_by == 999


def test_reject_draft_returns_400(client_with_db):
    """DRAFT 無法退簽。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "reviewer2", ["APPRAISAL_READ", "APPRAISAL_REVIEW"])
        s.commit()
        summary = _seed_summary(s, SummaryStatus.DRAFT)
        summary_id = summary.id
    _login(client, "reviewer2")
    r = client.post(
        f"/api/appraisal/summaries/{summary_id}/reject",
        json={"reason": "this should be impossible"},
    )
    assert r.status_code == 400


def test_reject_invalid_to_status(client_with_db):
    """SUPERVISOR_SIGNED 試圖退到 ACCOUNTING_SIGNED 應該拒絕（前進方向不是退簽）。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "reviewer3", ["APPRAISAL_READ", "APPRAISAL_REVIEW"])
        s.commit()
        summary = _seed_summary(s, SummaryStatus.SUPERVISOR_SIGNED)
        summary_id = summary.id
    _login(client, "reviewer3")
    r = client.post(
        f"/api/appraisal/summaries/{summary_id}/reject",
        json={
            "reason": "this is at least ten chars",
            "to_status": "ACCOUNTING_SIGNED",
        },
    )
    assert r.status_code in (400, 422)


def test_reject_reason_too_short(client_with_db):
    """reason < 10 字 → 422（Pydantic min_length 攔截）。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "reviewer4", ["APPRAISAL_READ", "APPRAISAL_REVIEW"])
        s.commit()
        summary = _seed_summary(s, SummaryStatus.SUPERVISOR_SIGNED)
        summary_id = summary.id
    _login(client, "reviewer4")
    r = client.post(
        f"/api/appraisal/summaries/{summary_id}/reject",
        json={"reason": "短"},
    )
    assert r.status_code == 422


def test_reject_requires_permission(client_with_db):
    """完全沒考核權限 → 403（endpoint 入口最低門檻是 APPRAISAL_READ）。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "noperm", [])
        s.commit()
        summary = _seed_summary(s, SummaryStatus.SUPERVISOR_SIGNED)
        summary_id = summary.id
    _login(client, "noperm")
    r = client.post(
        f"/api/appraisal/summaries/{summary_id}/reject",
        json={"reason": "this is a long enough reason"},
    )
    assert r.status_code == 403
