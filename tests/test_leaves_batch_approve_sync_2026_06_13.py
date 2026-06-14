"""P1-1 回歸：批次核准/駁回請假必須觸發 leave→attendance sync。

Bug（2026-06-13 深度 QA 發現）：
  單筆 approve_leave 核准時呼叫 sync.apply 在 Attendance 寫 status=LEAVE/
  partial_leave_hours 並設 leave_record_id；但 batch_approve_leaves 的 Pass2
  只 setattr status=APPROVED，全程無 sync.apply/revert。薪資引擎請假扣款唯一
  SoT 是 (Attendance, LeaveRecord) join on leave_record_id，沒同步的假單在
  join 中不存在 → 扣款為 0。HR「全選→批次核准」即整批漏扣扣薪假別。

對照單筆路徑測試 test_leaves_attendance_sync.py（I-1/I-2/I-3）。
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
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.leaves import router as leaves_router
from models.attendance import Attendance, AttendanceStatus
from models.database import Base, Employee, LeaveRecord, User
from utils.auth import hash_password


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """SQLite + TestClient + mocked salary engine（與 test_leaves_attendance_sync 對齊）。"""
    db_path = tmp_path / "batch-sync-test.sqlite"
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
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _setup_admin_and_employee(session_factory) -> int:
    with session_factory() as session:
        emp = Employee(
            employee_id="BSY001",
            name="批次同步測試員工",
            base_salary=36000,
            is_active=True,
        )
        session.add(emp)
        session.flush()
        emp_id = emp.id

        user = User(
            employee_id=None,
            username="batch_sync_admin",
            password_hash=hash_password("BatchSync123"),
            role="admin",
            permission_names=["*"],
            is_active=True,
            must_change_password=False,
        )
        session.add(user)
        session.commit()
    return emp_id


def _login(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": "batch_sync_admin", "password": "BatchSync123"},
    )
    assert resp.status_code == 200, f"login failed: {resp.json()}"


def _create_pending_leave(client: TestClient, employee_id: int, **kwargs) -> int:
    payload = {
        "employee_id": employee_id,
        "leave_type": kwargs.get("leave_type", "personal"),
        "start_date": kwargs.get("start_date", "2026-05-22"),
        "end_date": kwargs.get("end_date", "2026-05-22"),
        "leave_hours": kwargs.get("leave_hours", 8),
        "reason": "batch sync integration test",
    }
    resp = client.post("/api/leaves", json=payload)
    assert resp.status_code in (200, 201), f"create leave failed: {resp.text}"
    return resp.json()["id"]


def _batch_approve(client: TestClient, ids, approved: bool, **kwargs):
    body = {"ids": ids, "approved": approved}
    if not approved:
        body["rejection_reason"] = kwargs.get("rejection_reason", "批次測試駁回原因")
    return client.post("/api/leaves/batch-approve", json=body)


def test_batch_approve_writes_attendance_leave(app_client):
    """批次核准全天扣薪假 → Attendance 必有 status=LEAVE + leave_record_id（sync.apply 已跑）。"""
    client, session_factory = app_client
    emp_id = _setup_admin_and_employee(session_factory)
    _login(client)

    leave_id = _create_pending_leave(
        client, emp_id, start_date="2026-05-22", end_date="2026-05-22"
    )
    resp = _batch_approve(client, [leave_id], approved=True)
    assert resp.status_code == 200, f"batch approve failed: {resp.text}"

    with session_factory() as session:
        rows = (
            session.query(Attendance)
            .filter_by(employee_id=emp_id, leave_record_id=leave_id)
            .all()
        )
    assert (
        len(rows) == 1
    ), f"批次核准後應有 1 筆 Attendance（leave→attendance sync 已跑），實際 {len(rows)}"
    assert rows[0].status == AttendanceStatus.LEAVE.value


def test_batch_reject_of_approved_reverts_attendance(app_client):
    """已核准假單批次駁回 → revert 把 Attendance 還原（無打卡 → 刪 row）。"""
    client, session_factory = app_client
    emp_id = _setup_admin_and_employee(session_factory)
    _login(client)

    leave_id = _create_pending_leave(
        client, emp_id, start_date="2026-05-22", end_date="2026-05-22"
    )
    # 先批次核准（寫 attendance）
    resp = _batch_approve(client, [leave_id], approved=True)
    assert resp.status_code == 200, f"batch approve failed: {resp.text}"
    with session_factory() as session:
        assert (
            session.query(Attendance)
            .filter_by(employee_id=emp_id, leave_record_id=leave_id)
            .count()
            == 1
        ), "批次核准後應有 1 筆 Attendance"

    # 再批次駁回（revert）
    resp = _batch_approve(client, [leave_id], approved=False)
    assert resp.status_code == 200, f"batch reject failed: {resp.text}"

    with session_factory() as session:
        rows = (
            session.query(Attendance)
            .filter_by(employee_id=emp_id, leave_record_id=leave_id)
            .all()
        )
    assert rows == [], f"批次駁回後 Attendance 應 revert 全刪，實際 {len(rows)} 筆"
