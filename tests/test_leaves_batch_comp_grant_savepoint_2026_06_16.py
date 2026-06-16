"""Bug #3 回歸：批次核准補休 grant FIFO 扣抵/退回必須與 savepoint 同生共死。

Bug（2026-06-16 bug hunt 發現）：
  batch_approve_leaves（api/leaves.py）的 Pass2 把補休 grant 的
  _consume_compensatory_grants_fifo / _release_compensatory_grants_fifo 呼叫
  放在 `with session.begin_nested()` savepoint **之外**。一旦 savepoint 內的
  sync.apply / sync.revert 拋 LeaveAttendanceConflict / LeavePartialTimeMissing
  回滾，只會回滾「狀態翻面 + ApprovalLog + attendance 同步」，但 grant 的
  consumed_hours 變更（在 savepoint 之前就 += 了）不被回滾，最後整批 commit 落地
  → 假單仍是 pending（被列為 failed），補休帳本卻已被扣（少付）；對稱地，已核准
  批次駁回若 revert 衝突，grant 已 release 卻假單仍 approved（超發）。

  對照單筆 approve_leave 路徑（sync 先跑、衝突即 422，consume 在其後才執行，
  天然不會出現「扣了帳但假單沒核准」）。修法是把 consume/release 移進同一個
  savepoint，讓 ValueError/conflict 一致回滾。

本測試直接驅動真實 endpoint（與 test_leaves_batch_approve_sync_2026_06_13.py
  同構），monkeypatch sync.apply/revert 模擬衝突，斷言 grant.consumed_hours
  在回滾後維持原值。
"""

import os
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.leaves as leaves_module
import models.base as base_module
import services.employee_leave_attendance_sync as sync_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.leaves import router as leaves_router
from models.database import Base, Employee, LeaveQuota, LeaveRecord, User
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
from models.approval import ApprovalStatus
from utils.auth import hash_password


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """SQLite + TestClient + mocked salary engine。"""
    db_path = tmp_path / "batch-comp-savepoint.sqlite"
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


def _weekday_today() -> date:
    """回傳一個非週末的近期日期（預設工時 8h/天，避免排班校驗擋下）。"""
    d = date.today()
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


def _setup(session_factory, *, leave_status: str):
    """建立 admin、員工、補休配額、grant、一張補休假單，外加一張不同員工的「錨點」假單。

    錨點假單（非補休、不同員工）在同一批次內成功核准 → 觸發 session.commit()，
    這是讓 bug 浮出的關鍵：單筆 batch 失敗時不會 commit（finally close→implicit
    rollback 自然救回 grant），唯有同批存在成功條目才會把 savepoint 外的孤兒
    consumed_hours 變更一併 commit 落地。

    回傳 (emp_id, comp_leave_id, grant_id, anchor_leave_id)。
    """
    leave_day = _weekday_today()
    with session_factory() as session:
        emp = Employee(
            employee_id="CMP001",
            name="補休 savepoint 測試員工",
            base_salary=36000,
            is_active=True,
        )
        session.add(emp)
        anchor_emp = Employee(
            employee_id="ANC001",
            name="批次錨點員工",
            base_salary=36000,
            is_active=True,
        )
        session.add(anchor_emp)
        session.flush()
        emp_id = emp.id
        anchor_emp_id = anchor_emp.id

        user = User(
            employee_id=None,
            username="comp_savepoint_admin",
            password_hash=hash_password("CompSavepoint123"),
            role="admin",
            permission_names=["*"],
            is_active=True,
            must_change_password=False,
        )
        session.add(user)

        # 補休配額 row（legacy school_year=NULL，year=當年）讓核准期 quota 檢查放行
        session.add(
            LeaveQuota(
                employee_id=emp_id,
                year=leave_day.year,
                school_year=None,
                leave_type="compensatory",
                total_hours=8,
            )
        )

        # 一筆 active grant，足額可扣
        grant = OvertimeCompLeaveGrant(
            overtime_record_id=9001,
            employee_id=emp_id,
            granted_hours=8,
            granted_at=leave_day - timedelta(days=30),
            expires_at=leave_day + timedelta(days=300),
            consumed_hours=8 if leave_status == ApprovalStatus.APPROVED.value else 0,
            status="active",
        )
        session.add(grant)

        leave = LeaveRecord(
            employee_id=emp_id,
            leave_type="compensatory",
            start_date=leave_day,
            end_date=leave_day,
            leave_hours=8,
            status=leave_status,
            deduction_ratio=0.0,
            is_deductible=False,
            approved_by=(
                "管理員" if leave_status == ApprovalStatus.APPROVED.value else None
            ),
        )
        session.add(leave)

        # 錨點假單：與補休同樣的 leave_status，保證在同批走相同 approval_changed 流程並成功
        anchor_leave = LeaveRecord(
            employee_id=anchor_emp_id,
            leave_type="personal",
            start_date=leave_day,
            end_date=leave_day,
            leave_hours=8,
            status=leave_status,
            deduction_ratio=1.0,
            is_deductible=True,
            approved_by=(
                "管理員" if leave_status == ApprovalStatus.APPROVED.value else None
            ),
        )
        session.add(anchor_leave)
        session.commit()
        return emp_id, leave.id, grant.id, anchor_leave.id


def _login(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": "comp_savepoint_admin", "password": "CompSavepoint123"},
    )
    assert resp.status_code == 200, f"login failed: {resp.json()}"


def test_batch_approve_comp_grant_rolls_back_on_sync_conflict(app_client, monkeypatch):
    """批次核准補休：sync.apply 衝突回滾後，grant.consumed_hours 必須維持 0（未被扣抵）。

    未修前：consume 在 savepoint 之外先扣 8h，savepoint 因衝突回滾不影響它，
    整批 commit → grant.consumed_hours=8 卻假單仍 pending（少付）。
    """
    client, session_factory = app_client
    emp_id, leave_id, grant_id, anchor_id = _setup(
        session_factory, leave_status=ApprovalStatus.PENDING.value
    )
    _login(client)

    real_apply = sync_module.apply

    # 只讓補休那筆的 sync.apply 在 savepoint 內拋衝突；錨點假單照常成功 → 觸發 commit
    def _boom(session, lid):
        if lid == leave_id:
            raise sync_module.LeaveAttendanceConflict("模擬考勤衝突")
        return real_apply(session, lid)

    monkeypatch.setattr(sync_module, "apply", _boom)

    resp = client.post(
        "/api/leaves/batch-approve",
        json={"ids": [leave_id, anchor_id], "approved": True},
    )
    assert resp.status_code == 200, f"batch approve failed: {resp.text}"
    body = resp.json()
    # 補休那筆應失敗（sync 衝突），錨點那筆應成功（確保發生 commit）
    assert any(
        f["id"] == leave_id for f in body.get("failed", [])
    ), f"預期補休那筆因 sync 衝突 failed，實際 {body}"
    assert anchor_id in body.get(
        "succeeded", []
    ), f"預期錨點假單成功（觸發 commit），實際 {body}"

    with session_factory() as session:
        grant = session.get(OvertimeCompLeaveGrant, grant_id)
        leave = session.get(LeaveRecord, leave_id)
        assert grant.consumed_hours == 0, (
            f"sync 衝突回滾後 grant.consumed_hours 應維持 0（未扣抵），"
            f"實際 {grant.consumed_hours} → 補休帳本與假單脫鉤（少付）"
        )
        assert (
            leave.status == ApprovalStatus.PENDING.value
        ), f"sync 衝突後假單應維持 pending，實際 {leave.status}"


def test_batch_reject_of_approved_comp_grant_rolls_back_on_revert_conflict(
    app_client, monkeypatch
):
    """已核准補休批次駁回：sync.revert 衝突回滾後，grant.consumed_hours 必須維持原值（未被退回）。

    未修前：release 在 savepoint 之外先退 8h→0，savepoint 因 revert 衝突回滾不影響它，
    整批 commit → grant.consumed_hours=0 卻假單仍 approved（超發）。
    """
    client, session_factory = app_client
    emp_id, leave_id, grant_id, anchor_id = _setup(
        session_factory, leave_status=ApprovalStatus.APPROVED.value
    )
    _login(client)

    real_revert = sync_module.revert

    # 只讓補休那筆的 sync.revert 拋衝突；錨點假單照常 revert 成功 → 觸發 commit
    def _boom(session, lid):
        if lid == leave_id:
            raise sync_module.LeaveAttendanceConflict("模擬考勤 revert 衝突")
        return real_revert(session, lid)

    monkeypatch.setattr(sync_module, "revert", _boom)

    resp = client.post(
        "/api/leaves/batch-approve",
        json={
            "ids": [leave_id, anchor_id],
            "approved": False,
            "rejection_reason": "批次測試駁回原因",
        },
    )
    assert resp.status_code == 200, f"batch reject failed: {resp.text}"
    body = resp.json()
    assert any(
        f["id"] == leave_id for f in body.get("failed", [])
    ), f"預期補休那筆因 revert 衝突 failed，實際 {body}"
    assert anchor_id in body.get(
        "succeeded", []
    ), f"預期錨點假單成功（觸發 commit），實際 {body}"

    with session_factory() as session:
        grant = session.get(OvertimeCompLeaveGrant, grant_id)
        leave = session.get(LeaveRecord, leave_id)
        assert grant.consumed_hours == 8, (
            f"revert 衝突回滾後 grant.consumed_hours 應維持 8（未退回），"
            f"實際 {grant.consumed_hours} → 補休帳本與假單脫鉤（超發）"
        )
        assert (
            leave.status == ApprovalStatus.APPROVED.value
        ), f"revert 衝突後假單應維持 approved，實際 {leave.status}"
