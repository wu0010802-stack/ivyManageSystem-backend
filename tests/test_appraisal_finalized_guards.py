"""考核 FINALIZED 守衛：核定後不可改 cycle base/enrollment 或 recompute 已核定 summary。

威脅：bug sweep 2026-05-16 P0-2。
- recompute_summaries 沒擋 FINALIZED summary → 已核定獎金可被事後重算改金額
- update_cycle 沒擋 cycle 狀態 → 鎖定的週期 base_score 仍可被改，破壞稽核口徑
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
    db_path = tmp_path / "finalized-guard.sqlite"
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


APPRAISAL_ALL = (
    Permission.APPRAISAL_READ
    | Permission.APPRAISAL_EVENT_WRITE
    | Permission.APPRAISAL_REVIEW
    | Permission.APPRAISAL_ACCOUNTING
    | Permission.APPRAISAL_FINALIZE
)


def _login(client, username="admin", password="TempPass123"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text


def _seed_basic_cycle(sf, cycle_status=CycleStatus.OPEN):
    """建立 admin + cycle + 1 participant，回傳 (cycle_id, participant_id)。"""
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
            permissions=int(APPRAISAL_ALL),
            is_active=True,
        )
        s.add(admin_user)

        target_emp = Employee(
            employee_id="T001",
            name="被考核",
            employee_type=EmployeeType.REGULAR.value,
            is_active=True,
        )
        s.add(target_emp)
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
            employee_id=target_emp.id,
            role_group=RoleGroup.HEAD_TEACHER,
            is_excluded=False,
        )
        s.add(p)
        s.flush()
        s.commit()
        return cycle.id, p.id


class TestUpdateCycleFinalizedGuard:
    def test_update_base_score_blocked_when_locked(self, client_with_db):
        client, sf = client_with_db
        cycle_id, _ = _seed_basic_cycle(sf, cycle_status=CycleStatus.LOCKED)
        _login(client)
        res = client.patch(
            f"/api/appraisal/cycles/{cycle_id}",
            json={"base_score": 90.0},
        )
        assert res.status_code == 400, res.text
        assert "不允許修改" in res.json()["detail"]

    def test_update_enrollment_blocked_when_closed(self, client_with_db):
        client, sf = client_with_db
        cycle_id, _ = _seed_basic_cycle(sf, cycle_status=CycleStatus.CLOSED)
        _login(client)
        res = client.patch(
            f"/api/appraisal/cycles/{cycle_id}",
            json={"enrollment_actual": 50},
        )
        assert res.status_code == 400

    def test_update_status_only_allowed_when_locked(self, client_with_db):
        """只改 status（不動 base/enrollment）仍可通過，不要誤殺合法狀態切換。"""
        client, sf = client_with_db
        cycle_id, _ = _seed_basic_cycle(sf, cycle_status=CycleStatus.LOCKED)
        _login(client)
        res = client.patch(
            f"/api/appraisal/cycles/{cycle_id}",
            json={"status": "CLOSED"},
        )
        assert res.status_code == 200, res.text

    def test_update_base_score_allowed_when_open(self, client_with_db):
        client, sf = client_with_db
        cycle_id, _ = _seed_basic_cycle(sf, cycle_status=CycleStatus.OPEN)
        _login(client)
        res = client.patch(
            f"/api/appraisal/cycles/{cycle_id}",
            json={"base_score": 90.0},
        )
        assert res.status_code == 200, res.text


class TestRecomputeFinalizedSkip:
    def test_recompute_skips_finalized_summary(self, client_with_db):
        """已 FINALIZED 的 summary 不應被 recompute 覆寫。"""
        client, sf = client_with_db
        cycle_id, participant_id = _seed_basic_cycle(sf, cycle_status=CycleStatus.OPEN)
        with sf() as s:
            summary = AppraisalSummary(
                participant_id=participant_id,
                cycle_id=cycle_id,
                base_score=Decimal("70"),
                event_score_sum=Decimal("0"),
                total_score=Decimal("70"),
                grade=Grade.PASS,
                bonus_amount=Decimal("12345.67"),  # 標誌值，重算後不應變
                status=SummaryStatus.FINALIZED,
                version=3,
            )
            s.add(summary)
            s.commit()
            summary_id = summary.id

        _login(client)
        res = client.post(f"/api/appraisal/cycles/{cycle_id}/summaries:recompute")
        assert res.status_code == 200, res.text

        with sf() as s:
            after = s.query(AppraisalSummary).filter_by(id=summary_id).one()
            assert after.bonus_amount == Decimal(
                "12345.67"
            ), f"FINALIZED summary bonus_amount 被覆寫為 {after.bonus_amount}"
            assert after.version == 3, "FINALIZED summary 不應 bump version"
            assert after.status == SummaryStatus.FINALIZED

    def test_recompute_still_updates_draft_summary(self, client_with_db):
        """非 FINALIZED 的 summary 仍可正常 recompute（不要誤殺）。"""
        client, sf = client_with_db
        cycle_id, participant_id = _seed_basic_cycle(sf, cycle_status=CycleStatus.OPEN)
        with sf() as s:
            summary = AppraisalSummary(
                participant_id=participant_id,
                cycle_id=cycle_id,
                base_score=Decimal("70"),
                event_score_sum=Decimal("0"),
                total_score=Decimal("70"),
                grade=Grade.PASS,
                bonus_amount=Decimal("99999"),  # 不合理初值
                status=SummaryStatus.DRAFT,
                version=1,
            )
            s.add(summary)
            s.commit()
            summary_id = summary.id

        _login(client)
        res = client.post(f"/api/appraisal/cycles/{cycle_id}/summaries:recompute")
        assert res.status_code == 200

        with sf() as s:
            after = s.query(AppraisalSummary).filter_by(id=summary_id).one()
            # 重算後 bonus_amount 應從引擎產出（不應仍是 99999）
            assert after.bonus_amount != Decimal("99999")
            assert after.version == 2  # bump
