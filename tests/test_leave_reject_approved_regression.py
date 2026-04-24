"""
回歸測試：已核准假單被改駁回應觸發封存守衛與薪資重算

Bug 描述：
    PUT /leaves/{id}/approve 與 POST /leaves/batch-approve 在 approved=False 路徑
    只記錄狀態變更，未：
      (1) 檢查涉及月份是否已封存（_check_salary_months_not_finalized）
      (2) 觸發薪資重算（process_salary_calculation）

    實際情境：HR 先核准了假單（SalaryRecord 已扣款），之後誤判或政策變更改為駁回，
    系統只翻 is_approved，SalaryRecord 仍保留原扣款；並且即使該月薪資已封存，
    翻面動作仍會成功，DB 進入「假單已駁回但薪資仍有扣款」的矛盾狀態。

修復方式：
    以 was_approved != data.approved 作為「狀態變更需同步薪資」的判準，
    讓封存守衛與薪資重算同時涵蓋 approved→rejected 與 rejected→approved 兩條邊。
"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.leaves as leaves_module
import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.leaves import router as leaves_router
from models.database import Base, Employee, LeaveRecord, SalaryRecord, User
from utils.auth import hash_password


@pytest.fixture
def leave_reject_client(tmp_path, monkeypatch):
    db_path = tmp_path / "leave-reject-regression.sqlite"
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

    fake_salary_engine = MagicMock()
    monkeypatch.setattr(leaves_module, "_salary_engine", fake_salary_engine)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(leaves_router)

    with TestClient(app) as client:
        yield client, session_factory, fake_salary_engine

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_employee(session, employee_id: str, name: str) -> Employee:
    emp = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=36000,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _create_admin(session, username: str, password: str) -> User:
    user = User(
        employee_id=None,
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=-1,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _create_approved_leave(session, emp_id: int, leave_date: date) -> LeaveRecord:
    leave = LeaveRecord(
        employee_id=emp_id,
        leave_type="personal",  # deduction_ratio = 1.0
        start_date=leave_date,
        end_date=leave_date,
        leave_hours=8,
        is_approved=True,
        approved_by="admin",
        deduction_ratio=1.0,
        is_deductible=True,
    )
    session.add(leave)
    session.flush()
    return leave


class TestSingleRejectOfApprovedLeaveTriggersSalaryRecalc:
    """駁回一張原本已核准的假單，必須重算該月薪資。"""

    def test_reject_approved_leave_recalculates_salary(self, leave_reject_client):
        client, session_factory, fake_salary_engine = leave_reject_client
        leave_date = date(2026, 3, 10)
        with session_factory() as session:
            emp = _create_employee(session, "RJ001", "駁回教師")
            leave = _create_approved_leave(session, emp.id, leave_date)
            _create_admin(session, "rj_admin", "RjPass123")
            session.commit()
            leave_id = leave.id
            emp_id = emp.id

        assert _login(client, "rj_admin", "RjPass123").status_code == 200
        fake_salary_engine.reset_mock()

        res = client.put(
            f"/api/leaves/{leave_id}/approve",
            json={"approved": False, "rejection_reason": "實際未請"},
        )
        assert res.status_code == 200, res.json()

        with session_factory() as session:
            leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).one()
            assert leave.is_approved is False

        fake_salary_engine.process_salary_calculation.assert_any_call(
            emp_id, leave_date.year, leave_date.month
        )


class TestSingleRejectOfApprovedLeaveBlockedByFinalizedMonth:
    """駁回已核准假單若落在封存月，必須 409 阻擋，不得留下「假單翻面但薪資未更新」的矛盾狀態。"""

    def test_reject_approved_leave_on_finalized_month_returns_409(
        self, leave_reject_client
    ):
        client, session_factory, fake_salary_engine = leave_reject_client
        leave_date = date(2026, 3, 10)
        with session_factory() as session:
            emp = _create_employee(session, "RJ002", "封存月駁回教師")
            leave = _create_approved_leave(session, emp.id, leave_date)
            session.add(
                SalaryRecord(
                    employee_id=emp.id,
                    salary_year=leave_date.year,
                    salary_month=leave_date.month,
                    is_finalized=True,
                    finalized_by="財務",
                )
            )
            _create_admin(session, "rj_admin2", "RjPass123")
            session.commit()
            leave_id = leave.id

        assert _login(client, "rj_admin2", "RjPass123").status_code == 200
        fake_salary_engine.reset_mock()

        res = client.put(
            f"/api/leaves/{leave_id}/approve",
            json={"approved": False, "rejection_reason": "實際未請"},
        )
        assert res.status_code == 409, res.json()
        assert "封存" in res.json()["detail"]

        with session_factory() as session:
            leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).one()
            assert leave.is_approved is True, "封存守衛失敗時應保留原核准狀態"

        fake_salary_engine.process_salary_calculation.assert_not_called()


class TestBatchRejectOfApprovedLeaveTriggersSalaryRecalc:
    """批次駁回一張原本已核准的假單，必須重算該月薪資。"""

    def test_batch_reject_approved_leave_recalculates_salary(self, leave_reject_client):
        client, session_factory, fake_salary_engine = leave_reject_client
        leave_date = date(2026, 3, 11)
        with session_factory() as session:
            emp = _create_employee(session, "RJ003", "批次駁回教師")
            leave = _create_approved_leave(session, emp.id, leave_date)
            _create_admin(session, "rj_admin3", "RjPass123")
            session.commit()
            leave_id = leave.id
            emp_id = emp.id

        assert _login(client, "rj_admin3", "RjPass123").status_code == 200
        fake_salary_engine.reset_mock()

        res = client.post(
            "/api/leaves/batch-approve",
            json={
                "ids": [leave_id],
                "approved": False,
                "rejection_reason": "批次駁回",
            },
        )
        assert res.status_code == 200, res.json()
        body = res.json()
        assert leave_id in body["succeeded"], body

        with session_factory() as session:
            leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).one()
            assert leave.is_approved is False

        fake_salary_engine.process_salary_calculation.assert_any_call(
            emp_id, leave_date.year, leave_date.month
        )


class TestBatchRejectOfApprovedLeaveBlockedByFinalizedMonth:
    """批次駁回若涉及封存月，該筆應進 failed 清單、不翻 is_approved、不重算。"""

    def test_batch_reject_approved_leave_on_finalized_month_fails(
        self, leave_reject_client
    ):
        client, session_factory, fake_salary_engine = leave_reject_client
        leave_date = date(2026, 3, 12)
        with session_factory() as session:
            emp = _create_employee(session, "RJ004", "批次封存月駁回教師")
            leave = _create_approved_leave(session, emp.id, leave_date)
            session.add(
                SalaryRecord(
                    employee_id=emp.id,
                    salary_year=leave_date.year,
                    salary_month=leave_date.month,
                    is_finalized=True,
                    finalized_by="財務",
                )
            )
            _create_admin(session, "rj_admin4", "RjPass123")
            session.commit()
            leave_id = leave.id

        assert _login(client, "rj_admin4", "RjPass123").status_code == 200
        fake_salary_engine.reset_mock()

        res = client.post(
            "/api/leaves/batch-approve",
            json={
                "ids": [leave_id],
                "approved": False,
                "rejection_reason": "批次駁回",
            },
        )
        assert res.status_code == 200, res.json()
        body = res.json()
        assert leave_id not in body["succeeded"], body
        failed_ids = [f["id"] for f in body["failed"]]
        assert leave_id in failed_ids, body
        reason = next(f["reason"] for f in body["failed"] if f["id"] == leave_id)
        assert "封存" in str(reason)

        with session_factory() as session:
            leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).one()
            assert leave.is_approved is True, "封存守衛失敗時應保留原核准狀態"

        fake_salary_engine.process_salary_calculation.assert_not_called()
