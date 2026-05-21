"""GET /cycles/{id}/sign_status_summary 3 case 測試（Phase 2 Task 10）。

威脅：聚合各 status 計數時要排除 is_excluded participant；
counts 鍵需涵蓋全部 SummaryStatus（零值補 0）；cycle 不存在要 404。

fixture pattern 沿用 tests/test_appraisal_batch_sign_endpoint.py。
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
    db_path = tmp_path / "appraisal-sign-status-summary-endpoint.sqlite"
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


def _seed_n_summaries(s, n, status=SummaryStatus.DRAFT):
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
    ids = []
    for i in range(n):
        emp = Employee(
            employee_id=f"E{i:03d}",
            name=f"員工{i}",
            employee_type=EmployeeType.REGULAR.value,
            is_active=True,
        )
        s.add(emp)
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
        s.flush()
        ids.append(summary.id)
    s.commit()
    return cycle, ids


# ===== 3 case =====


def test_sign_status_summary_groups_by_status(client_with_db):
    """4 個 summary：1 DRAFT、2 SUPERVISOR_SIGNED、1 FINALIZED。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "viewer1", Permission.APPRAISAL_READ)
        s.commit()
        cycle, ids = _seed_n_summaries(s, 4, SummaryStatus.DRAFT)
        cycle_id = cycle.id
        s.query(AppraisalSummary).filter_by(id=ids[0]).update(
            {"status": SummaryStatus.SUPERVISOR_SIGNED}
        )
        s.query(AppraisalSummary).filter_by(id=ids[1]).update(
            {"status": SummaryStatus.SUPERVISOR_SIGNED}
        )
        s.query(AppraisalSummary).filter_by(id=ids[2]).update(
            {"status": SummaryStatus.FINALIZED}
        )
        s.commit()
    _login(client, "viewer1")
    r = client.get(f"/api/appraisal/cycles/{cycle_id}/sign_status_summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cycle_id"] == cycle_id
    assert body["counts"]["DRAFT"] == 1
    assert body["counts"]["SUPERVISOR_SIGNED"] == 2
    assert body["counts"]["ACCOUNTING_SIGNED"] == 0
    assert body["counts"]["FINALIZED"] == 1
    buckets = {b["status"]: b for b in body["buckets"]}
    assert len(buckets["SUPERVISOR_SIGNED"]["summaries"]) == 2
    # bucket 內 summary 含 employee_name
    assert all(
        "employee_name" in item for item in buckets["SUPERVISOR_SIGNED"]["summaries"]
    )


def test_excludes_is_excluded_participants(client_with_db):
    """is_excluded=True 的 participant 對應 summary 不算入。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "viewer2", Permission.APPRAISAL_READ)
        s.commit()
        cycle, ids = _seed_n_summaries(s, 2, SummaryStatus.DRAFT)
        cycle_id = cycle.id
        # 把 ids[0] 對應的 participant 標為 excluded
        summary = s.get(AppraisalSummary, ids[0])
        summary.participant.is_excluded = True
        s.commit()
    _login(client, "viewer2")
    r = client.get(f"/api/appraisal/cycles/{cycle_id}/sign_status_summary")
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["DRAFT"] == 1


def test_404_for_unknown_cycle(client_with_db):
    """cycle 不存在 → 404。"""
    client, sf = client_with_db
    with sf() as s:
        _create_user(s, "viewer3", Permission.APPRAISAL_READ)
        s.commit()
    _login(client, "viewer3")
    r = client.get("/api/appraisal/cycles/99999/sign_status_summary")
    assert r.status_code == 404
