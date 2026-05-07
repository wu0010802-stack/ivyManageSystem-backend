"""驗證 overtimes approve 端點駁回時必填 rejection_reason，落 ApprovalLog.comment。

audit 2026-05-07 P1：
- overtimes 駁回不必填原因（對照 leaves.py:1106 / punch_corrections.py:149 都已硬性必填）
- OvertimeRecord 無 rejection_reason 欄位（LeaveRecord 才有），改 ApprovalLog.comment 落

flip-flop 守衛提案撤回：
業主既有業務模型允許 reject_of_approved 路徑（發現超時數需撤銷），由
risk_tags 在 audit_summary 打標記，不擋。如未來需要更嚴限制，應改為
「reject_of_approved 強制 ACTIVITY_PAYMENT_APPROVE + reason ≥ 10 字」軟擋方案。
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.overtimes import router as overtimes_router
from models.database import (
    ApprovalLog,
    ApprovalPolicy,
    Base,
    Employee,
    OvertimeRecord,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "ot_reject.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
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
    app.include_router(overtimes_router)

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_emp(session, *, emp_no, name="員工"):
    e = Employee(employee_id=emp_no, name=name, base_salary=36000, is_active=True)
    session.add(e)
    session.flush()
    return e


def _seed_user(session, *, username, role, employee_id, permissions):
    u = User(
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role=role,
        permissions=permissions,
        is_active=True,
        must_change_password=False,
        employee_id=employee_id,
    )
    session.add(u)
    session.flush()
    return u


def _seed_policy(session, *, doc_type, submitter_role, approver_roles):
    p = ApprovalPolicy(
        doc_type=doc_type,
        submitter_role=submitter_role,
        approver_roles=approver_roles,
        is_active=True,
    )
    session.add(p)
    session.flush()
    return p


def _seed_overtime(session, *, employee_id, is_approved=None):
    today = date.today()
    ot = OvertimeRecord(
        employee_id=employee_id,
        overtime_date=today - timedelta(days=2),
        overtime_type="weekday",
        hours=2.0,
        start_time=datetime(2026, 5, 5, 18, 0),
        end_time=datetime(2026, 5, 5, 20, 0),
        is_approved=is_approved,
    )
    session.add(ot)
    session.flush()
    return ot


def _login(client, username):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": "Passw0rd!"},
    )


APPROVE_OT = (
    int(Permission.APPROVALS)
    | int(Permission.OVERTIME_WRITE)
    | int(Permission.OVERTIME_READ)
)


class TestOvertimeRejectionReasonRequired:
    def _setup(self, sf):
        with sf() as s:
            sub_emp = _seed_emp(s, emp_no="E_OT_REJ_S")
            sup_emp = _seed_emp(s, emp_no="E_OT_REJ_SUP")
            _seed_user(
                s,
                username="ot_rej_sub",
                role="teacher",
                employee_id=sub_emp.id,
                permissions=int(Permission.OVERTIME_READ),
            )
            _seed_user(
                s,
                username="ot_rej_sup",
                role="supervisor",
                employee_id=sup_emp.id,
                permissions=APPROVE_OT,
            )
            _seed_policy(
                s,
                doc_type="overtime",
                submitter_role="teacher",
                approver_roles="supervisor,admin",
            )
            ot = _seed_overtime(s, employee_id=sub_emp.id, is_approved=None)
            s.commit()
            return ot.id

    def test_reject_without_reason_blocked(self, client):
        c, sf = client
        ot_id = self._setup(sf)

        assert _login(c, "ot_rej_sup").status_code == 200
        res = c.put(
            f"/api/overtimes/{ot_id}/approve",
            params={"approved": "false"},
        )
        assert res.status_code == 400, res.text
        assert "原因" in res.json()["detail"]

    def test_reject_with_empty_reason_blocked(self, client):
        c, sf = client
        ot_id = self._setup(sf)

        assert _login(c, "ot_rej_sup").status_code == 200
        res = c.put(
            f"/api/overtimes/{ot_id}/approve",
            params={"approved": "false", "rejection_reason": "  "},
        )
        assert res.status_code == 400, res.text

    def test_reject_with_short_reason_blocked(self, client):
        c, sf = client
        ot_id = self._setup(sf)

        assert _login(c, "ot_rej_sup").status_code == 200
        res = c.put(
            f"/api/overtimes/{ot_id}/approve",
            params={"approved": "false", "rejection_reason": "AB"},
        )
        assert res.status_code == 400

    def test_reject_with_valid_reason_passes_and_logs_comment(self, client):
        c, sf = client
        ot_id = self._setup(sf)

        assert _login(c, "ot_rej_sup").status_code == 200
        res = c.put(
            f"/api/overtimes/{ot_id}/approve",
            params={"approved": "false", "rejection_reason": "時數不符"},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            ot = s.get(OvertimeRecord, ot_id)
            assert ot.is_approved is False
            log = (
                s.query(ApprovalLog)
                .filter(
                    ApprovalLog.doc_type == "overtime",
                    ApprovalLog.doc_id == ot_id,
                    ApprovalLog.action == "rejected",
                )
                .first()
            )
            assert log is not None
            assert log.comment == "時數不符"

    def test_approve_without_reason_passes(self, client):
        c, sf = client
        ot_id = self._setup(sf)

        assert _login(c, "ot_rej_sup").status_code == 200
        res = c.put(
            f"/api/overtimes/{ot_id}/approve",
            params={"approved": "true"},
        )
        assert res.status_code == 200, res.text

    def test_reject_of_approved_still_allowed_with_reason(self, client):
        """既有業務允許 reject_of_approved（reject 已 approved 單），由
        audit_summary 打 risk_tag。本測試確認此路徑未被 hard-block。"""
        c, sf = client
        with sf() as s:
            sub_emp = _seed_emp(s, emp_no="E_OT_RA_S")
            sup_emp = _seed_emp(s, emp_no="E_OT_RA_SUP")
            _seed_user(
                s,
                username="ot_ra_sub",
                role="teacher",
                employee_id=sub_emp.id,
                permissions=int(Permission.OVERTIME_READ),
            )
            _seed_user(
                s,
                username="ot_ra_sup",
                role="supervisor",
                employee_id=sup_emp.id,
                permissions=APPROVE_OT,
            )
            _seed_policy(
                s,
                doc_type="overtime",
                submitter_role="teacher",
                approver_roles="supervisor,admin",
            )
            ot = _seed_overtime(s, employee_id=sub_emp.id, is_approved=True)
            s.commit()
            ot_id = ot.id

        assert _login(c, "ot_ra_sup").status_code == 200
        res = c.put(
            f"/api/overtimes/{ot_id}/approve",
            params={"approved": "false", "rejection_reason": "事後審核發現問題"},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            ot = s.get(OvertimeRecord, ot_id)
            assert ot.is_approved is False
