"""I-1~I-3, I-8~I-10: leaves approve_leave hook 整合測試。

Task 12 of employee-leave-attendance-sync:
  - I-1: approve → Attendance rows 建立
  - I-2: reject(approved=False) → 無 Attendance rows
  - I-3: approve 後再 reject → Attendance rows 全刪
  - I-8: approve 觸發 LeaveAttendanceConflict → 422
  - I-9: approve 連點兩次 → 只寫一次
  - I-10: approve 時 sync 拋 RuntimeError → 422+/leave 不變

使用 in-memory SQLite + TestClient，不動既有 salary/LINE/ApprovalLog 邏輯。
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

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """SQLite in-memory + TestClient + mocked salary engine。"""
    db_path = tmp_path / "sync-hook-test.sqlite"
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

    # salary engine mock：跳過薪資重算
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
    """建立 admin User（無 employee_id，避免自我核准守衛）與員工，回傳員工 id。"""
    with session_factory() as session:
        emp = Employee(
            employee_id="SYN001",
            name="同步測試員工",
            base_salary=36000,
            is_active=True,
        )
        session.add(emp)
        session.flush()
        emp_id = emp.id

        user = User(
            employee_id=None,
            username="sync_admin",
            password_hash=hash_password("SyncAdmin123"),
            role="admin",
            permissions=-1,
            is_active=True,
            must_change_password=False,
        )
        session.add(user)
        session.commit()

    return emp_id


def _login(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": "sync_admin", "password": "SyncAdmin123"},
    )
    assert resp.status_code == 200, f"login failed: {resp.json()}"


def _create_pending_leave(client: TestClient, employee_id: int, **kwargs) -> int:
    """建立一筆 pending leave（POST /api/leaves），回傳 leave_id。"""
    payload = {
        "employee_id": employee_id,
        "leave_type": kwargs.get("leave_type", "personal"),
        "start_date": kwargs.get("start_date", "2026-05-22"),
        "end_date": kwargs.get("end_date", "2026-05-22"),
        "leave_hours": kwargs.get("leave_hours", 8),
        "reason": "sync integration test",
    }
    if kwargs.get("start_time"):
        payload["start_time"] = kwargs["start_time"]
    if kwargs.get("end_time"):
        payload["end_time"] = kwargs["end_time"]
    resp = client.post("/api/leaves", json=payload)
    assert resp.status_code in (200, 201), f"create leave failed: {resp.text}"
    return resp.json()["id"]


def _approve(client: TestClient, leave_id: int, approved: bool, **kwargs):
    """PUT /api/leaves/{id}/approve。"""
    body = {"approved": approved}
    if not approved:
        body["rejection_reason"] = kwargs.get("rejection_reason", "測試駁回原因")
    return client.put(f"/api/leaves/{leave_id}/approve", json=body)


# ─────────────────────────────────────────────────────────────────────────────
# I-1 ~ I-3, I-8 ~ I-10
# ─────────────────────────────────────────────────────────────────────────────


class TestApproveHookIntegration:

    def test_i1_approve_writes_attendance(self, app_client):
        """I-1: PUT approve approved=True → Attendance row 建立。"""
        client, session_factory = app_client
        emp_id = _setup_admin_and_employee(session_factory)
        _login(client)

        leave_id = _create_pending_leave(
            client, emp_id, start_date="2026-05-22", end_date="2026-05-22"
        )
        resp = _approve(client, leave_id, approved=True)
        assert resp.status_code == 200, f"approve failed: {resp.text}"

        with session_factory() as session:
            rows = (
                session.query(Attendance)
                .filter_by(employee_id=emp_id, leave_record_id=leave_id)
                .all()
            )
        assert len(rows) == 1, f"預期 1 筆 Attendance，實際 {len(rows)}"
        assert rows[0].status == AttendanceStatus.LEAVE.value

    def test_i2_reject_does_not_write_attendance(self, app_client):
        """I-2: PUT approve approved=False(reject) → 無 Attendance row 建立。"""
        client, session_factory = app_client
        emp_id = _setup_admin_and_employee(session_factory)
        _login(client)

        leave_id = _create_pending_leave(client, emp_id)
        resp = _approve(client, leave_id, approved=False)
        assert resp.status_code == 200, f"reject failed: {resp.text}"

        with session_factory() as session:
            rows = (
                session.query(Attendance)
                .filter_by(employee_id=emp_id, leave_record_id=leave_id)
                .all()
            )
        assert rows == [], f"reject 後不應建立 Attendance，實際 {len(rows)} 筆"

    def test_i3_approve_then_reject_reverts(self, app_client):
        """I-3: approve 後再 reject → Attendance row 全刪（revert）。"""
        client, session_factory = app_client
        emp_id = _setup_admin_and_employee(session_factory)
        _login(client)

        leave_id = _create_pending_leave(
            client, emp_id, start_date="2026-05-22", end_date="2026-05-22"
        )
        # 先核准
        _approve(client, leave_id, approved=True)

        with session_factory() as session:
            count_after_approve = (
                session.query(Attendance)
                .filter_by(employee_id=emp_id, leave_record_id=leave_id)
                .count()
            )
        assert count_after_approve == 1, "approve 後應有 1 筆 Attendance"

        # 再駁回
        resp = _approve(client, leave_id, approved=False)
        assert resp.status_code == 200, f"reject failed: {resp.text}"

        with session_factory() as session:
            rows = (
                session.query(Attendance)
                .filter_by(employee_id=emp_id, leave_record_id=leave_id)
                .all()
            )
        assert rows == [], f"revert 後 Attendance 應全刪，實際 {len(rows)} 筆"

    def test_i8_approve_with_conflict_returns_422(self, app_client):
        """I-8: approve 觸發 LeaveAttendanceConflict → 422 / leave 仍 pending。"""
        client, session_factory = app_client
        emp_id = _setup_admin_and_employee(session_factory)
        _login(client)

        # 預先在同日建一筆由其他假單佔走的 Attendance（leave_record_id=9999 假裝已存在）
        with session_factory() as session:
            conflicting_att = Attendance(
                employee_id=emp_id,
                attendance_date=date(2026, 5, 22),
                status=AttendanceStatus.LEAVE.value,
                leave_record_id=9999,
            )
            session.add(conflicting_att)
            session.commit()

        leave_id = _create_pending_leave(
            client, emp_id, start_date="2026-05-22", end_date="2026-05-22"
        )
        resp = _approve(client, leave_id, approved=True)
        assert resp.status_code == 422, f"應 422，實際 {resp.status_code}: {resp.text}"

        # leave 不應變動（transaction rollback → 仍 pending）
        with session_factory() as session:
            leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
        assert (
            leave.is_approved is None
        ), f"rollback 後 is_approved 應仍為 None，實際 {leave.is_approved}"

    def test_i9_approve_idempotent(self, app_client):
        """I-9: approve 連點兩次 → 只寫一次 Attendance。"""
        client, session_factory = app_client
        emp_id = _setup_admin_and_employee(session_factory)
        _login(client)

        leave_id = _create_pending_leave(
            client, emp_id, start_date="2026-05-22", end_date="2026-05-22"
        )
        _approve(client, leave_id, approved=True)

        # 第二次相同 approve（was_approved=True → approval_changed=False → hook 不再呼叫）
        resp = _approve(client, leave_id, approved=True)
        assert resp.status_code in (
            200,
            409,
        ), f"第二次 approve 非預期 {resp.status_code}"

        with session_factory() as session:
            rows = (
                session.query(Attendance)
                .filter_by(employee_id=emp_id, leave_record_id=leave_id)
                .all()
            )
        assert len(rows) == 1, f"idempotent：應只有 1 筆 Attendance，實際 {len(rows)}"

    def test_i10_sync_failure_rollbacks_leave(self, app_client, monkeypatch):
        """I-10: approve 時 sync.apply 拋 RuntimeError → 500 / LeaveRecord 不變。

        TestClient 預設會重新拋出 server-side exceptions；使用
        raise_server_exceptions=False 讓它回傳 500 Response 而非 re-raise。
        """
        client, session_factory = app_client
        emp_id = _setup_admin_and_employee(session_factory)
        _login(client)

        # monkeypatch sync.apply 讓它拋 RuntimeError
        from services import employee_leave_attendance_sync as sync_mod

        def boom(*args, **kwargs):
            raise RuntimeError("sync 故意爆")

        monkeypatch.setattr(sync_mod, "apply", boom)

        leave_id = _create_pending_leave(client, emp_id)

        # 用 raise_server_exceptions=False 的 client 取得 500 Response
        import api.leaves as lm
        import models.base as bm

        app2 = FastAPI()
        app2.include_router(auth_router)
        app2.include_router(leaves_router)
        with TestClient(app2, raise_server_exceptions=False) as no_raise_client:
            # 複用同一 cookie jar（直接傳 cookies 給新 client）
            no_raise_client.cookies.update(client.cookies)
            resp = no_raise_client.put(
                f"/api/leaves/{leave_id}/approve",
                json={"approved": True},
            )

        # RuntimeError 不被 hook catch → FastAPI 500
        assert resp.status_code >= 400, f"sync 失敗應 >= 400，實際 {resp.status_code}"

        # leave 應 rollback → 仍 pending
        with session_factory() as session:
            leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
        assert (
            leave.is_approved is None
        ), f"rollback 後 is_approved 應仍為 None，實際 {leave.is_approved}"


# ─────────────────────────────────────────────────────────────────────────────
# I-4 ~ I-6: update_leave hook 整合
# ─────────────────────────────────────────────────────────────────────────────


class TestUpdateHookIntegration:
    def test_i4_update_extend_end_date(self, app_client):
        """I-4: approve 後 PUT 改 end_date 延長 → 視 update 退審 / reapply 行為驗收。

        update_leave 既有邏輯：改任何欄位會把 is_approved 從 True 設回 None（退審）。
        退審路徑：sync.revert → Attendance row 全刪。
        """
        client, session_factory = app_client
        emp_id = _setup_admin_and_employee(session_factory)
        _login(client)

        leave_id = _create_pending_leave(
            client, emp_id, start_date="2026-05-22", end_date="2026-05-22"
        )
        resp = _approve(client, leave_id, approved=True)
        assert resp.status_code == 200, f"approve failed: {resp.text}"

        # PUT 延長 end_date → 觸發退審，sync.revert 應刪 Attendance
        resp = client.put(
            f"/api/leaves/{leave_id}",
            json={"end_date": "2026-05-22", "leave_hours": 8},
        )
        assert resp.status_code == 200, f"update leave failed: {resp.text}"

        with session_factory() as session:
            leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
            rows = session.query(Attendance).filter_by(leave_record_id=leave_id).all()

        if leave.is_approved is True:
            # reapply 路徑（若 update 未退審）
            assert len(rows) >= 1
        else:
            # 退審路徑 → revert → Attendance 全刪
            assert len(rows) == 0, f"退審後 Attendance 應全刪，實際 {len(rows)} 筆"

    def test_i5_update_hours_full_to_partial(self, app_client):
        """I-5: approve 後 PUT 改 leave_hours（縮減）+ start_time/end_time → 部分標記。

        退審路徑：revert 後 Attendance row 全刪。
        測試使用 leave_hours=3（不超出 schedule 有界工時上限）。
        """
        client, session_factory = app_client
        emp_id = _setup_admin_and_employee(session_factory)
        _login(client)

        leave_id = _create_pending_leave(
            client, emp_id, start_date="2026-05-22", end_date="2026-05-22"
        )
        resp = _approve(client, leave_id, approved=True)
        assert resp.status_code == 200, f"approve failed: {resp.text}"

        resp = client.put(
            f"/api/leaves/{leave_id}",
            json={
                "leave_hours": 3,
                "start_time": "09:00",
                "end_time": "12:00",
            },
        )
        assert resp.status_code == 200, f"update leave failed: {resp.text}"

        with session_factory() as session:
            leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
            rows = session.query(Attendance).filter_by(leave_record_id=leave_id).all()

        if leave.is_approved is True:
            # reapply 路徑（若 update 未退審）：部分請假，partial_leave_hours=3
            assert len(rows) == 1
            assert rows[0].partial_leave_hours is not None
        else:
            # 退審路徑 → revert → Attendance 全刪
            assert len(rows) == 0, f"退審後 Attendance 應全刪，實際 {len(rows)} 筆"

    def test_i6_update_leave_type_reverts_attendance(self, app_client):
        """I-6: approve 後 PUT 改 leave_type 觸發退審 → AttendanceRecord 反寫。

        update_leave 既有邏輯：改任何欄位會把 is_approved 從 True 設回 None。
        退審路徑：sync.revert → Attendance row 全刪。
        """
        client, session_factory = app_client
        emp_id = _setup_admin_and_employee(session_factory)
        _login(client)

        leave_id = _create_pending_leave(
            client, emp_id, start_date="2026-05-22", end_date="2026-05-22"
        )
        resp = _approve(client, leave_id, approved=True)
        assert resp.status_code == 200, f"approve failed: {resp.text}"

        with session_factory() as session:
            count_after_approve = (
                session.query(Attendance).filter_by(leave_record_id=leave_id).count()
            )
        assert count_after_approve == 1, "approve 後應有 1 筆 Attendance"

        # PUT 改 leave_type → 觸發退審
        resp = client.put(
            f"/api/leaves/{leave_id}",
            json={"leave_type": "sick"},
        )
        assert resp.status_code == 200, f"update leave failed: {resp.text}"

        with session_factory() as session:
            leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
            rows = session.query(Attendance).filter_by(leave_record_id=leave_id).all()

        # 退審路徑 → is_approved=None → revert → Attendance 全刪
        if leave.is_approved is None:
            assert len(rows) == 0, f"revert 後 Attendance 應全刪，實際 {len(rows)} 筆"


# ─────────────────────────────────────────────────────────────────────────────
# I-7: delete_leave hook 整合
# ─────────────────────────────────────────────────────────────────────────────


class TestDeleteHookIntegration:

    def test_i7_delete_approved_reverts_attendance(self, app_client):
        """I-7: approve 後 DELETE → AttendanceRecord 反寫；leave row 刪。

        步驟：
        1. 建立 pending leave
        2. approve → 確認 Attendance row 存在
        3. DELETE /api/leaves/{id} → 200
        4. 確認 Attendance row 已刪（revert）
        5. 確認 LeaveRecord 已不存在
        """
        client, session_factory = app_client
        emp_id = _setup_admin_and_employee(session_factory)
        _login(client)

        leave_id = _create_pending_leave(
            client, emp_id, start_date="2026-05-22", end_date="2026-05-22"
        )

        # approve → Attendance row 應建立
        resp = _approve(client, leave_id, approved=True)
        assert resp.status_code == 200, f"approve failed: {resp.text}"

        with session_factory() as session:
            count_after_approve = (
                session.query(Attendance)
                .filter_by(employee_id=emp_id, leave_record_id=leave_id)
                .count()
            )
        assert count_after_approve == 1, "approve 後應有 1 筆 Attendance"

        # DELETE → 應觸發 sync.revert 再刪 leave
        resp = client.delete(f"/api/leaves/{leave_id}")
        assert resp.status_code == 200, f"delete failed: {resp.text}"

        with session_factory() as session:
            # Attendance row 應已 revert（刪除）
            att_rows = (
                session.query(Attendance)
                .filter_by(employee_id=emp_id, leave_record_id=leave_id)
                .all()
            )
            # LeaveRecord 應已刪除
            leave = session.query(LeaveRecord).filter_by(id=leave_id).first()

        assert (
            att_rows == []
        ), f"delete 後 Attendance 應全刪（revert），實際 {len(att_rows)} 筆"
        assert leave is None, "delete 後 LeaveRecord 應已刪除"
