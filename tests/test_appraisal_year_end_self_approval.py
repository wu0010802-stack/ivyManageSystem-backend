"""考核 / 年終獎金三層簽核守衛：禁止自簽自己的獎金（403）。

威脅：bug sweep 2026-05-16 P1-2。原本六處 sign_supervisor / sign_accounting /
finalize_(summary|settlement) 沒有比對 current_user.employee_id ↔
participant.employee_id（or settlement.employee_id），園長/主任可以自己加獎金後自簽通過。

修法：utils.approval_helpers.assert_not_self_approval helper + 6 處套用。
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
from api.year_end import year_end_router
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
from models.year_end import (
    EmployeeYearEndSnapshot,
    YearEndCycle,
    YearEndCycleStatus,
    YearEndSettlement,
    YearEndSettlementStatus,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "self-approval.sqlite"
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
    app.include_router(year_end_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user_with_employee(
    session, username, perms, employee_id, password="TempPass123"
):
    if isinstance(perms, str):
        perms = [perms]
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permission_names=perms,
        is_active=True,
        employee_id=employee_id,
    )
    session.add(user)
    session.flush()
    return user


def _make_employee(session, name, eid):
    emp = Employee(
        employee_id=eid,
        name=name,
        employee_type=EmployeeType.REGULAR.value,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _login(client, username, password="TempPass123"):
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text
    return res


# 完整簽核權限
APPRAISAL_ALL = [
    "APPRAISAL_READ",
    "APPRAISAL_EVENT_WRITE",
    "APPRAISAL_REVIEW",
    "APPRAISAL_ACCOUNTING",
    "APPRAISAL_FINALIZE",
]
YEAR_END_ALL = [
    "YEAR_END_READ",
    "YEAR_END_WRITE",
    "YEAR_END_FINALIZE",
    # 年終簽核端點實際用 APPRAISAL_REVIEW / APPRAISAL_ACCOUNTING 權限位元
    "APPRAISAL_REVIEW",
    "APPRAISAL_ACCOUNTING",
]


def _seed_appraisal_summary(sf, target_status: SummaryStatus):
    """建立一份 summary，回傳 (summary_id, self_employee_id, other_employee_id)。"""
    with sf() as s:
        # 兩個員工：boss 自己 + other 別人
        boss_emp = _make_employee(s, "園長", "B001")
        other_emp = _make_employee(s, "別人", "O001")
        # boss 兼任 user
        _create_user_with_employee(s, "boss", APPRAISAL_ALL, boss_emp.id)
        _create_user_with_employee(s, "other_signer", APPRAISAL_ALL, other_emp.id)

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

        # boss 本人是 participant
        p = AppraisalParticipant(
            cycle_id=cycle.id,
            employee_id=boss_emp.id,
            role_group=RoleGroup.SUPERVISOR,
            is_excluded=False,
        )
        s.add(p)
        s.flush()

        summary = AppraisalSummary(
            participant_id=p.id,
            cycle_id=cycle.id,
            base_score=Decimal("80"),
            total_score=Decimal("80"),
            grade=Grade.PASS,
            bonus_amount=Decimal("8000"),
            status=target_status,
        )
        s.add(summary)
        s.flush()
        s.commit()
        return summary.id, boss_emp.id, other_emp.id


class TestAppraisalSelfApproval:
    def test_sign_supervisor_rejects_self(self, client_with_db):
        client, sf = client_with_db
        summary_id, _, _ = _seed_appraisal_summary(sf, SummaryStatus.DRAFT)
        _login(client, "boss")
        res = client.post(f"/api/appraisal/summaries/{summary_id}/sign_supervisor")
        assert res.status_code == 403, res.text
        assert "自行簽核" in res.json()["detail"]

    def test_sign_accounting_rejects_self(self, client_with_db):
        client, sf = client_with_db
        summary_id, _, _ = _seed_appraisal_summary(sf, SummaryStatus.SUPERVISOR_SIGNED)
        _login(client, "boss")
        res = client.post(f"/api/appraisal/summaries/{summary_id}/sign_accounting")
        assert res.status_code == 403, res.text

    def test_finalize_rejects_self(self, client_with_db):
        client, sf = client_with_db
        summary_id, _, _ = _seed_appraisal_summary(sf, SummaryStatus.ACCOUNTING_SIGNED)
        _login(client, "boss")
        res = client.post(f"/api/appraisal/summaries/{summary_id}/finalize")
        assert res.status_code == 403, res.text

    def test_sign_supervisor_allows_other(self, client_with_db):
        """別人簽 boss 的單，正常通過 → 確認守衛沒誤殺。"""
        client, sf = client_with_db
        summary_id, _, _ = _seed_appraisal_summary(sf, SummaryStatus.DRAFT)
        _login(client, "other_signer")
        res = client.post(f"/api/appraisal/summaries/{summary_id}/sign_supervisor")
        assert res.status_code == 200, res.text


def _seed_year_end_settlement(sf, target_status: YearEndSettlementStatus):
    with sf() as s:
        boss_emp = _make_employee(s, "園長", "BY01")
        other_emp = _make_employee(s, "別人", "OY01")
        third_emp = _make_employee(s, "第三人", "TY01")
        _create_user_with_employee(s, "boss_ye", YEAR_END_ALL, boss_emp.id)
        _create_user_with_employee(s, "other_ye_signer", YEAR_END_ALL, other_emp.id)
        _create_user_with_employee(s, "third_ye_signer", YEAR_END_ALL, third_emp.id)

        cycle = YearEndCycle(
            academic_year=114,
            start_date=date(2025, 8, 1),
            end_date=date(2026, 7, 31),
            bonus_calc_date=date(2026, 1, 15),
            status=YearEndCycleStatus.OPEN,
        )
        s.add(cycle)
        s.flush()

        snapshot = EmployeeYearEndSnapshot(
            year_end_cycle_id=cycle.id,
            employee_id=boss_emp.id,
            base_salary=Decimal("40000"),
            festival_total=Decimal("0"),
            hire_months=Decimal("12"),
        )
        s.add(snapshot)
        s.flush()

        settlement = YearEndSettlement(
            year_end_cycle_id=cycle.id,
            employee_id=boss_emp.id,
            snapshot_id=snapshot.id,
            total_amount=Decimal("50000"),
            status=target_status,
        )
        s.add(settlement)
        s.flush()
        s.commit()
        return settlement.id


class TestYearEndSelfApproval:
    def test_sign_supervisor_rejects_self(self, client_with_db):
        client, sf = client_with_db
        sid = _seed_year_end_settlement(sf, YearEndSettlementStatus.DRAFT)
        _login(client, "boss_ye")
        res = client.post(f"/api/year_end/settlements/{sid}/sign_supervisor")
        assert res.status_code == 403, res.text

    def test_sign_accounting_rejects_self(self, client_with_db):
        client, sf = client_with_db
        sid = _seed_year_end_settlement(sf, YearEndSettlementStatus.SUPERVISOR_SIGNED)
        _login(client, "boss_ye")
        res = client.post(f"/api/year_end/settlements/{sid}/sign_accounting")
        assert res.status_code == 403, res.text

    def test_finalize_rejects_self(self, client_with_db):
        client, sf = client_with_db
        sid = _seed_year_end_settlement(sf, YearEndSettlementStatus.ACCOUNTING_SIGNED)
        _login(client, "boss_ye")
        res = client.post(f"/api/year_end/settlements/{sid}/finalize")
        assert res.status_code == 403, res.text

    def test_sign_supervisor_allows_other(self, client_with_db):
        client, sf = client_with_db
        sid = _seed_year_end_settlement(sf, YearEndSettlementStatus.DRAFT)
        _login(client, "other_ye_signer")
        res = client.post(f"/api/year_end/settlements/{sid}/sign_supervisor")
        assert res.status_code == 200, res.text


class TestYearEndSignSegregation:
    """pentest E2：年終簽核職責分離（不可跳主管關、同一人不可連做兩關）。"""

    def _url(self, sid, stage):
        return f"/api/year_end/settlements/{sid}/{stage}"

    def test_two_gate_flow_allowed_and_blocks_same_signer(self, client_with_db):
        # 2-gate（會計從 DRAFT 直簽 → 核定）為設計支援流程；
        # 但同一人不可既簽會計又核定（職責分離）。
        client, sf = client_with_db
        sid = _seed_year_end_settlement(sf, YearEndSettlementStatus.DRAFT)
        _login(client, "other_ye_signer")  # A 會計從 DRAFT 直簽（合法）
        assert client.post(self._url(sid, "sign_accounting")).status_code == 200
        res = client.post(self._url(sid, "finalize"))  # A 又核定 → 擋
        assert res.status_code == 403, res.text

    def test_two_gate_distinct_signers_succeeds(self, client_with_db):
        # 2-gate 正向：會計(A)從 DRAFT 直簽 → 核定(B≠A) 成功
        client, sf = client_with_db
        sid = _seed_year_end_settlement(sf, YearEndSettlementStatus.DRAFT)
        _login(client, "other_ye_signer")  # A
        assert client.post(self._url(sid, "sign_accounting")).status_code == 200
        _login(client, "third_ye_signer")  # B
        res = client.post(self._url(sid, "finalize"))
        assert res.status_code == 200, res.text

    def test_same_person_supervisor_then_accounting_rejected(self, client_with_db):
        # E2：同一人簽完主管關不能再簽會計關
        client, sf = client_with_db
        sid = _seed_year_end_settlement(sf, YearEndSettlementStatus.DRAFT)
        _login(client, "other_ye_signer")
        assert client.post(self._url(sid, "sign_supervisor")).status_code == 200
        res = client.post(self._url(sid, "sign_accounting"))
        assert res.status_code == 403, res.text

    def test_finalize_rejects_same_as_accounting(self, client_with_db):
        # E2：核定人不能就是會計簽核人
        client, sf = client_with_db
        sid = _seed_year_end_settlement(sf, YearEndSettlementStatus.DRAFT)
        _login(client, "other_ye_signer")
        assert client.post(self._url(sid, "sign_supervisor")).status_code == 200
        _login(client, "third_ye_signer")
        assert client.post(self._url(sid, "sign_accounting")).status_code == 200
        res = client.post(self._url(sid, "finalize"))  # third 同時是會計
        assert res.status_code == 403, res.text

    def test_full_flow_two_distinct_signers_succeeds(self, client_with_db):
        # 正向：主管(A)→會計(B≠A)→核定(A≠B) 兩個不同人即可完成（maker-checker）
        client, sf = client_with_db
        sid = _seed_year_end_settlement(sf, YearEndSettlementStatus.DRAFT)
        _login(client, "other_ye_signer")  # A
        assert client.post(self._url(sid, "sign_supervisor")).status_code == 200
        _login(client, "third_ye_signer")  # B
        assert client.post(self._url(sid, "sign_accounting")).status_code == 200
        _login(client, "other_ye_signer")  # A 核定（≠B 會計）
        res = client.post(self._url(sid, "finalize"))
        assert res.status_code == 200, res.text
