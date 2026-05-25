"""sign 進階成功時清掉舊 rejected_* 殘影（P1-5）。

威脅：reject 後 summary 留下 rejected_at / rejected_by / rejected_from_stage /
rejected_reason，後續再從同階段往上簽，這四個欄位仍殘留 → UI 把已成功進階
的 summary 顯示為「曾被退簽」，造成混淆。

修補：在 sign_supervisor / sign_accounting / finalize_summary 三個單筆端點
以及 batch_sign 三個 stage 分支成功時，呼叫 clear_rejection_state(summary)。
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone
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
    db_path = tmp_path / "appraisal-sign-clear-rejection.sqlite"
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


def _seed_summary_with_rejection_residue(s, status=SummaryStatus.SUPERVISOR_SIGNED):
    """建一個 status=SUPERVISOR_SIGNED 但帶舊 rejected_* 殘影的 summary。

    模擬流程：曾從 FINALIZED reject 回 ACCOUNTING_SIGNED，再 reject 回
    SUPERVISOR_SIGNED；殘影應在下次 sign 進階時清掉。
    """
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
    # 帶 supervisor 簽核殘影（之前 signed 過、被退、又 sign 過）
    summary.supervisor_signed_by = 999
    summary.supervisor_signed_at = datetime.now(timezone.utc)
    # rejected_* 殘影
    summary.rejected_at = datetime.now(timezone.utc)
    summary.rejected_by = 888
    summary.rejected_from_stage = SummaryStatus.FINALIZED
    summary.rejected_reason = "舊的退簽原因——應在下次 sign 進階時被清掉"
    s.add(summary)
    s.commit()
    return summary


def test_sign_accounting_clears_rejected_fields(client_with_db):
    """從 SUPERVISOR_SIGNED 簽到 ACCOUNTING_SIGNED，rejected_* 應全 None。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_ACCOUNTING)
        summary = _seed_summary_with_rejection_residue(
            s, status=SummaryStatus.SUPERVISOR_SIGNED
        )
        sid = summary.id
    _login(client, "admin1")
    r = client.post(f"/api/appraisal/summaries/{sid}/sign_accounting")
    assert r.status_code == 200, r.text
    with sf() as s:
        after = s.get(AppraisalSummary, sid)
        assert after.status == SummaryStatus.ACCOUNTING_SIGNED
        assert after.rejected_at is None
        assert after.rejected_by is None
        assert after.rejected_from_stage is None
        assert after.rejected_reason is None


def test_sign_supervisor_clears_rejected_fields(client_with_db):
    """從 DRAFT 簽到 SUPERVISOR_SIGNED，rejected_* 應全 None。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_REVIEW)
        summary = _seed_summary_with_rejection_residue(s, status=SummaryStatus.DRAFT)
        # DRAFT 不該有 supervisor_signed_by 殘影，但 rejected_* 仍可能殘留
        summary.supervisor_signed_by = None
        summary.supervisor_signed_at = None
        sid = summary.id
        # commit 殘留 rejected_*
        s.commit()
    _login(client, "admin1")
    r = client.post(f"/api/appraisal/summaries/{sid}/sign_supervisor")
    assert r.status_code == 200, r.text
    with sf() as s:
        after = s.get(AppraisalSummary, sid)
        assert after.status == SummaryStatus.SUPERVISOR_SIGNED
        assert after.rejected_at is None
        assert after.rejected_by is None
        assert after.rejected_from_stage is None
        assert after.rejected_reason is None


def test_finalize_clears_rejected_fields(client_with_db):
    """從 ACCOUNTING_SIGNED 簽到 FINALIZED，rejected_* 應全 None。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "admin1", Permission.APPRAISAL_FINALIZE)
        summary = _seed_summary_with_rejection_residue(
            s, status=SummaryStatus.ACCOUNTING_SIGNED
        )
        summary.accounting_signed_by = 999
        summary.accounting_signed_at = datetime.now(timezone.utc)
        sid = summary.id
        s.commit()
    _login(client, "admin1")
    r = client.post(f"/api/appraisal/summaries/{sid}/finalize")
    assert r.status_code == 200, r.text
    with sf() as s:
        after = s.get(AppraisalSummary, sid)
        assert after.status == SummaryStatus.FINALIZED
        assert after.rejected_at is None
        assert after.rejected_by is None
        assert after.rejected_from_stage is None
        assert after.rejected_reason is None


def test_batch_sign_clears_rejected_fields(client_with_db):
    """batch_sign SUPERVISOR 也要清舊 rejected_* 殘影。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(
            s,
            "admin1",
            ["APPRAISAL_READ", "APPRAISAL_REVIEW"],
        )
        s.commit()
        summary = _seed_summary_with_rejection_residue(s, status=SummaryStatus.DRAFT)
        summary.supervisor_signed_by = None
        summary.supervisor_signed_at = None
        sid = summary.id
        cycle_id = summary.cycle_id
        s.commit()
    _login(client, "admin1")
    r = client.post(
        f"/api/appraisal/cycles/{cycle_id}/summaries:batch_sign",
        json={"summary_ids": [sid], "stage": "SUPERVISOR"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert sid in body["succeeded"]
    with sf() as s:
        after = s.get(AppraisalSummary, sid)
        assert after.status == SummaryStatus.SUPERVISOR_SIGNED
        assert after.rejected_at is None
        assert after.rejected_by is None
        assert after.rejected_from_stage is None
        assert after.rejected_reason is None
