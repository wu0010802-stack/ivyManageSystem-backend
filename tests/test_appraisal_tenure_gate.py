"""考核到職月數守衛：到職未滿 2 月跳過不計考核（對齊 Excel「未滿/未簽約不計算考核」）。

測試案例：
- 到職 1.0 月 → recompute 後沒有 AppraisalSummary
- 到職 5.0 月 → recompute 後有 AppraisalSummary
- 到職 1.0 月 + 既有 stale summary → recompute 後 stale summary 被刪除
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
from sqlalchemy.pool import NullPool

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

APPRAISAL_ALL = [
    "APPRAISAL_READ",
    "APPRAISAL_EVENT_WRITE",
    "APPRAISAL_REVIEW",
    "APPRAISAL_ACCOUNTING",
    "APPRAISAL_FINALIZE",
]


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "tenure-gate.sqlite"
    # NullPool ensures every session gets a fresh connection — avoids SQLite
    # read-snapshot isolation hiding committed DELETEs from a subsequent session
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
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
        yield client, session_factory, db_path

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login(client, username="admin", password="TempPass123"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text


def _seed_cycle_two_participants(
    sf, hire_months_a=Decimal("1.0"), hire_months_b=Decimal("5.0")
):
    """建立 admin + cycle + 2 participants，回傳 (cycle_id, participant_id_a, participant_id_b)。
    A: hire_months_in_cycle = hire_months_a（預設 1.0，未滿 2 月，應跳過）
    B: hire_months_in_cycle = hire_months_b（預設 5.0，已滿 2 月，應計算）
    """
    with sf() as s:
        admin_emp = Employee(
            employee_id="A001",
            name="管理員",
            employee_type=EmployeeType.REGULAR.value,
            is_active=True,
        )
        s.add(admin_emp)
        s.flush()
        admin_user = User(
            username="admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permission_names=APPRAISAL_ALL,
            is_active=True,
        )
        s.add(admin_user)

        emp_a = Employee(
            employee_id="EA01",
            name="新到職員工",
            employee_type=EmployeeType.REGULAR.value,
            is_active=True,
        )
        emp_b = Employee(
            employee_id="EB01",
            name="老員工",
            employee_type=EmployeeType.REGULAR.value,
            is_active=True,
        )
        s.add_all([emp_a, emp_b])
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

        p_a = AppraisalParticipant(
            cycle_id=cycle.id,
            employee_id=emp_a.id,
            role_group=RoleGroup.HEAD_TEACHER,
            hire_months_in_cycle=hire_months_a,
            is_excluded=False,
        )
        p_b = AppraisalParticipant(
            cycle_id=cycle.id,
            employee_id=emp_b.id,
            role_group=RoleGroup.HEAD_TEACHER,
            hire_months_in_cycle=hire_months_b,
            is_excluded=False,
        )
        s.add_all([p_a, p_b])
        s.flush()
        s.commit()
        return cycle.id, p_a.id, p_b.id


def test_short_tenure_participant_gets_no_summary(client_with_db):
    """到職 1.0 月（< 2）→ recompute 後 A 沒有 AppraisalSummary。"""
    client, sf, _db_path = client_with_db
    cycle_id, p_a_id, p_b_id = _seed_cycle_two_participants(sf)
    _login(client)

    res = client.post(f"/api/appraisal/cycles/{cycle_id}/summaries:recompute")
    assert res.status_code == 200, res.text

    with sf() as s:
        summary_a = s.query(AppraisalSummary).filter_by(participant_id=p_a_id).first()
        assert summary_a is None, "到職未滿 2 月的 participant 不應有 AppraisalSummary"


def test_sufficient_tenure_participant_gets_summary(client_with_db):
    """到職 5.0 月（>= 2）→ recompute 後 B 有 AppraisalSummary。"""
    client, sf, _db_path = client_with_db
    cycle_id, p_a_id, p_b_id = _seed_cycle_two_participants(sf)
    _login(client)

    res = client.post(f"/api/appraisal/cycles/{cycle_id}/summaries:recompute")
    assert res.status_code == 200, res.text

    with sf() as s:
        summary_b = s.query(AppraisalSummary).filter_by(participant_id=p_b_id).first()
        assert (
            summary_b is not None
        ), "到職已滿 2 月的 participant 應有 AppraisalSummary"


def test_stale_summary_deleted_for_short_tenure(client_with_db):
    """到職 1.0 月 + 既有 stale summary → recompute 後 stale summary 被刪除。"""
    import sqlite3

    client, sf, db_path = client_with_db
    cycle_id, p_a_id, p_b_id = _seed_cycle_two_participants(sf)

    # 預先插入一個 stale summary for participant A
    with sf() as s:
        stale = AppraisalSummary(
            participant_id=p_a_id,
            cycle_id=cycle_id,
            base_score=Decimal("75.6"),
            event_score_sum=Decimal("0"),
            total_score=Decimal("75.6"),
            grade=Grade.PASS,
            bonus_amount=Decimal("9999"),  # 標誌值
            status=SummaryStatus.DRAFT,
        )
        s.add(stale)
        s.commit()
        stale_id = stale.id

    _login(client)
    res = client.post(f"/api/appraisal/cycles/{cycle_id}/summaries:recompute")
    assert res.status_code == 200, res.text

    # Raw sqlite3 ground-truth check — bypasses SA session/pool entirely
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT id FROM appraisal_summaries WHERE id=?", (stale_id,)
    ).fetchone()
    conn.close()
    assert (
        row is None
    ), f"recompute 應刪除到職未滿 2 月 participant 的既有 stale summary，但 sqlite3 仍見 id={row}"
