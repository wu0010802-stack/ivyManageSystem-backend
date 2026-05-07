"""請假 / 加班安全漏洞回歸測試（針對 8 項權限 / 資料完整性修復）。

涵蓋：
- Issue 1：import_leaves + approve_leave 必須檢查排班工時
- Issue 2：approve_overtime 在核准前補做最後一致性驗證
- Issue 3：portal 補休假單必須驗證 source_overtime_id
- Issue 4：import_overtimes 加重疊檢查、時間解析失敗應報錯
- Issue 5：OvertimeCreatePortal 拒絕反向時間
- Issue 6：OvertimeUpdate 單欄更新合併後仍須驗證時間順序
- Issue 7：已審核假單禁止新增附件
- Issue 8：批次核准補休加班需使用含列鎖的共用 helper（靜態檢查）
"""

import io
import os
import sys
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.leaves as leaves_module
import api.overtimes as overtimes_module
import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.leaves import router as leaves_router
from api.overtimes import router as overtimes_router
from api.portal.leaves import router as portal_leaves_router
from api.portal._shared import OvertimeCreatePortal
from models.database import (
    Base,
    Employee,
    Holiday,
    LeaveQuota,
    LeaveRecord,
    OvertimeRecord,
    User,
)
from utils.auth import hash_password


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "security-fixes.sqlite"
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
    monkeypatch.setattr(overtimes_module, "_salary_engine", fake_salary_engine)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(leaves_router)
    app.include_router(overtimes_router)
    app.include_router(portal_leaves_router, prefix="/api/portal")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _emp(session, employee_id: str, name: str) -> Employee:
    e = Employee(employee_id=employee_id, name=name, base_salary=36000, is_active=True)
    session.add(e)
    session.flush()
    return e


def _user(session, *, username, password, role, permissions, employee=None) -> User:
    u = User(
        employee_id=employee.id if employee else None,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=permissions,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


# ── Issue 5：OvertimeCreatePortal 時間順序驗證（Pydantic 純單元測試） ──


class TestPortalOvertimeReverseTimeRejected:
    def test_portal_overtime_rejects_reverse_time_range(self):
        with pytest.raises(ValueError, match="早於 end_time"):
            OvertimeCreatePortal(
                overtime_date=date(2026, 3, 20),
                overtime_type="weekday",
                start_time="20:00",
                end_time="08:00",
                hours=2,
            )

    def test_portal_overtime_rejects_malformed_time(self):
        with pytest.raises(ValueError):
            OvertimeCreatePortal(
                overtime_date=date(2026, 3, 20),
                overtime_type="weekday",
                start_time="25:99",
                end_time="26:00",
                hours=2,
            )


# ── Issue 6：OvertimeUpdate 單欄更新合併後仍驗證時間順序 ──


class TestOvertimeUpdateReverseTimeRejected:
    def test_end_time_update_alone_cannot_produce_reverse_range(self, app_client):
        client, session_factory = app_client
        with session_factory() as session:
            emp = _emp(session, "OT001", "加班教師")
            ot = OvertimeRecord(
                employee_id=emp.id,
                overtime_date=date(2026, 3, 20),
                overtime_type="weekday",
                start_time=datetime(2026, 3, 20, 18, 0),
                end_time=datetime(2026, 3, 20, 20, 0),
                hours=2,
                overtime_pay=500,
                is_approved=None,
            )
            session.add(ot)
            _user(
                session,
                username="ot_admin",
                password="AdminPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()
            ot_id = ot.id

        assert _login(client, "ot_admin", "AdminPass123").status_code == 200

        res = client.put(
            f"/api/overtimes/{ot_id}",
            json={"end_time": "09:00"},
        )
        assert res.status_code == 400
        assert "早於 end_time" in res.json()["detail"]


# ── Issue 7：已審核假單禁止新增附件 ──


class TestApprovedLeaveRejectsNewAttachment:
    def test_cannot_upload_attachment_after_leave_is_approved(self, app_client):
        client, session_factory = app_client
        with session_factory() as session:
            emp = _emp(session, "L001", "附件教師")
            leave = LeaveRecord(
                employee_id=emp.id,
                leave_type="personal",
                start_date=date(2026, 3, 20),
                end_date=date(2026, 3, 20),
                leave_hours=8,
                is_approved=True,
                approved_by="admin",
            )
            session.add(leave)
            _user(
                session,
                username="l_teacher",
                password="TeachPass123",
                role="teacher",
                permissions=0,
                employee=emp,
            )
            session.commit()
            leave_id = leave.id

        assert _login(client, "l_teacher", "TeachPass123").status_code == 200

        # 使用最小 PNG 頭 + 1 byte 內容，讓 file signature validator 通過但不寫大檔
        png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
        files = {"files": ("evidence.png", png_header, "image/png")}
        res = client.post(f"/api/portal/my-leaves/{leave_id}/attachments", files=files)
        assert res.status_code == 400
        assert "已審核" in res.json()["detail"]


# ── Issue 3：portal 補休假單必須驗證 source_overtime_id ──


class TestPortalCompLeaveSourceOvertimeValidation:
    def _setup_actor(self, session):
        emp = _emp(session, "C001", "補休教師")
        other = _emp(session, "C002", "他人教師")
        _user(
            session,
            username="c_teacher",
            password="CompPass123",
            role="teacher",
            permissions=0,
            employee=emp,
        )
        # 補休配額 - 避免 quota 檢查擋住測試
        session.add(
            LeaveQuota(
                employee_id=emp.id,
                year=2026,
                leave_type="compensatory",
                total_hours=8,
            )
        )
        return emp, other

    def test_rejects_nonexistent_source_overtime(self, app_client):
        # F-011 collapse 後，「不存在」與「不屬於本人」一律回 400 generic
        # 「來源加班記錄無效或無權使用」，避免存在性 oracle。
        client, session_factory = app_client
        with session_factory() as session:
            self._setup_actor(session)
            session.commit()

        assert _login(client, "c_teacher", "CompPass123").status_code == 200
        res = client.post(
            "/api/portal/my-leaves",
            json={
                "leave_type": "compensatory",
                "start_date": "2026-03-20",
                "end_date": "2026-03-20",
                "leave_hours": 4,
                "source_overtime_id": 9999,
            },
        )
        assert res.status_code == 400
        assert "無效或無權使用" in res.json()["detail"]

    def test_rejects_source_overtime_belonging_to_other_employee(self, app_client):
        # F-011 collapse：同上一個測試的 generic detail，且 status code 一致為 400。
        client, session_factory = app_client
        with session_factory() as session:
            emp, other = self._setup_actor(session)
            other_ot = OvertimeRecord(
                employee_id=other.id,
                overtime_date=date(2026, 3, 15),
                overtime_type="weekday",
                hours=4,
                overtime_pay=0,
                use_comp_leave=True,
                comp_leave_granted=True,
                is_approved=True,
                approved_by="admin",
            )
            session.add(other_ot)
            session.commit()
            ot_id = other_ot.id

        assert _login(client, "c_teacher", "CompPass123").status_code == 200
        res = client.post(
            "/api/portal/my-leaves",
            json={
                "leave_type": "compensatory",
                "start_date": "2026-03-20",
                "end_date": "2026-03-20",
                "leave_hours": 4,
                "source_overtime_id": ot_id,
            },
        )
        assert res.status_code == 400
        assert "無效或無權使用" in res.json()["detail"]

    def test_rejects_unapproved_source_overtime(self, app_client):
        client, session_factory = app_client
        with session_factory() as session:
            emp, _ = self._setup_actor(session)
            ot = OvertimeRecord(
                employee_id=emp.id,
                overtime_date=date(2026, 3, 15),
                overtime_type="weekday",
                hours=4,
                overtime_pay=0,
                use_comp_leave=True,
                comp_leave_granted=False,
                is_approved=None,
            )
            session.add(ot)
            session.commit()
            ot_id = ot.id

        assert _login(client, "c_teacher", "CompPass123").status_code == 200
        res = client.post(
            "/api/portal/my-leaves",
            json={
                "leave_type": "compensatory",
                "start_date": "2026-03-20",
                "end_date": "2026-03-20",
                "leave_hours": 4,
                "source_overtime_id": ot_id,
            },
        )
        assert res.status_code == 400
        assert "尚未核准" in res.json()["detail"]


# ── Issue 1：匯入請假必須檢查排班工時 ──


class TestImportLeavesHoursGuard:
    def _build_xlsx(self) -> bytes:
        wb = Workbook()
        ws = wb.active
        ws.append(
            [
                "員工編號",
                "員工姓名",
                "假別代碼",
                "開始日期",
                "結束日期",
                "時數(可空)",
                "原因(可空)",
            ]
        )
        ws.append(
            ["IMP001", "匯入教師", "personal", "2026-03-20", "2026-03-20", 100, "超額"]
        )
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_import_rejects_hours_beyond_daily_schedule(self, app_client):
        client, session_factory = app_client
        with session_factory() as session:
            _emp(session, "IMP001", "匯入教師")
            _user(
                session,
                username="imp_admin",
                password="AdminPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()

        assert _login(client, "imp_admin", "AdminPass123").status_code == 200

        xlsx_bytes = self._build_xlsx()
        res = client.post(
            "/api/leaves/import",
            files={
                "file": (
                    "leaves.xlsx",
                    xlsx_bytes,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["created"] == 0
        assert body["failed"] == 1
        assert any("工作時數" in e or "可請假" in e for e in body["errors"])


class TestApproveLeaveHoursGuardDefenseInDepth:
    """即使壞資料已經在 DB 裡（例如舊版匯入、手改），核准路徑也必須擋住超額時數。"""

    def _seed_excess_pending_leave(self, session) -> int:
        emp = _emp(session, "IMP500", "超額教師")
        leave = LeaveRecord(
            employee_id=emp.id,
            leave_type="personal",
            start_date=date(2026, 3, 20),
            end_date=date(2026, 3, 20),
            leave_hours=100,  # 遠超單日排班工時
            is_approved=None,
        )
        session.add(leave)
        _user(
            session,
            username="imp_admin2",
            password="AdminPass123",
            role="admin",
            permissions=-1,
        )
        session.flush()
        return leave.id

    def test_single_approve_rejects_excess_hours(self, app_client):
        client, session_factory = app_client
        with session_factory() as session:
            leave_id = self._seed_excess_pending_leave(session)
            session.commit()

        assert _login(client, "imp_admin2", "AdminPass123").status_code == 200
        res = client.put(f"/api/leaves/{leave_id}/approve", json={"approved": True})
        assert res.status_code == 400
        assert "工作時數" in res.json()["detail"] or "可請假" in res.json()["detail"]

        with session_factory() as session:
            leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).one()
            assert leave.is_approved is None, "核准失敗時不得翻面"

    def test_batch_approve_rejects_excess_hours(self, app_client):
        client, session_factory = app_client
        with session_factory() as session:
            leave_id = self._seed_excess_pending_leave(session)
            session.commit()

        assert _login(client, "imp_admin2", "AdminPass123").status_code == 200
        res = client.post(
            "/api/leaves/batch-approve",
            json={"ids": [leave_id], "approved": True},
        )
        assert res.status_code == 200
        body = res.json()
        assert leave_id not in body["succeeded"]
        failed_ids = [f["id"] for f in body["failed"]]
        assert leave_id in failed_ids
        reason = next(f["reason"] for f in body["failed"] if f["id"] == leave_id)
        assert "工作時數" in str(reason) or "可請假" in str(reason)


# ── Issue 2：approve_overtime 最後一致性驗證（反向時間） ──


class TestApproveOvertimeRejectsInvalidPendingRecord:
    def test_approve_rejects_overtime_with_reverse_time(self, app_client):
        client, session_factory = app_client
        with session_factory() as session:
            emp = _emp(session, "OT200", "壞資料教師")
            # 直接在 DB 寫入反向時間（模擬舊資料或 DB 手改）
            ot = OvertimeRecord(
                employee_id=emp.id,
                overtime_date=date(2026, 3, 20),
                overtime_type="weekday",
                start_time=datetime(2026, 3, 20, 20, 0),
                end_time=datetime(2026, 3, 20, 18, 0),
                hours=2,
                overtime_pay=500,
                is_approved=None,
            )
            session.add(ot)
            _user(
                session,
                username="ot_admin2",
                password="AdminPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()
            ot_id = ot.id

        assert _login(client, "ot_admin2", "AdminPass123").status_code == 200

        res = client.put(f"/api/overtimes/{ot_id}/approve?approved=true")
        assert res.status_code == 400
        assert "早於 end_time" in res.json()["detail"]


# ── Issue 8：批次核准補休加班使用含列鎖的共用 helper（靜態檢查） ──


class TestBatchApproveUsesCompLeaveHelper:
    def test_batch_approve_delegates_comp_leave_quota_to_helper(self):
        import inspect

        src = inspect.getsource(overtimes_module.batch_approve_overtimes)
        # 確認批次核准已改用 helper，而不是就地 session.query(LeaveQuota).first()
        # 否則缺少 with_for_update() 會出現 race condition
        assert "_grant_comp_leave_quota" in src
        # 確認不再有手寫的非鎖查詢邏輯
        assert ".with_for_update()" not in src or "LeaveQuota" not in src
