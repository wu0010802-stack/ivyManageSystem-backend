"""W-1, W-2: create_or_update_attendance_record 接 merge helper 整合測試
W-3: upload_attendance Excel 批次匯入接 merge helper 整合測試

W-1: admin 對已有 approved full-day leave 的日期手動補打卡 → leave_record_id 仍對齊
W-2: admin 重複編輯同一 row → leave_record_id 保留
W-3: approve 半天請假 09:00-13:00 → Excel upload 該日 punch_in=09:30
     → leave_record_id 對齊 / partial_leave_hours=4 / late=0
"""

import os
import sys
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.attendance import router as attendance_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.base import Base
from models.database import Attendance, Employee, LeaveRecord, User
from utils.auth import hash_password
from utils.attendance_leave_merge import merge_attendance_with_leave
from utils.permissions import Permission

# ── Fixtures ───────────────────────────────────────────────────────────

ATT_PERMS = int(Permission.ATTENDANCE_READ) | int(Permission.ATTENDANCE_WRITE)


@pytest.fixture
def att_client(tmp_path):
    """建立隔離的 sqlite 測試 app（attendance write leave-aware 用）。"""
    db_path = tmp_path / "att-leave-aware.sqlite"
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
    app.include_router(attendance_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _make_employee(session, *, employee_id: str, name: str) -> Employee:
    emp = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=30000,
        employee_type="regular",
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_user(
    session,
    *,
    username: str,
    permissions: int,
    employee_id: int | None = None,
    role: str = "admin",
) -> User:
    user = User(
        username=username,
        password_hash=hash_password("Temp123456"),
        role=role,
        permissions=permissions,
        employee_id=employee_id,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient, username: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": "Temp123456"}
    )


def _approve_full_day_leave(session, emp_id: int, leave_date: date) -> LeaveRecord:
    """建立一筆全天 approved leave（無 start_time/end_time）。"""
    lv = LeaveRecord(
        employee_id=emp_id,
        leave_type="personal",
        start_date=leave_date,
        end_date=leave_date,
        leave_hours=8.0,
        start_time=None,
        end_time=None,
        is_approved=True,
    )
    session.add(lv)
    session.flush()
    return lv


# ══════════════════════════════════════════════════════════════════════
# TestAdminWriteWithLeave
# ══════════════════════════════════════════════════════════════════════


class TestAdminWriteWithLeave:
    def test_w1_post_attendance_with_approved_full_day_leave(self, att_client):
        """W-1: approved full-day leave 後 admin 對該日手動補打卡
        → response 201, leave_record_id 欄位對齊（DB row 寫入正確）。
        """
        client, sf = att_client
        leave_date = date(2026, 5, 22)

        with sf() as s:
            # admin 帳號（無 employee_id，可寫任何人）
            _make_user(s, username="admin_w1", permissions=ATT_PERMS, employee_id=None)
            # 目標員工
            target = _make_employee(s, employee_id="E_W1", name="W1員工")
            # 建立 approved full-day leave
            lv = _approve_full_day_leave(s, target.id, leave_date)
            s.commit()
            target_id = target.id
            leave_id = lv.id

        assert _login(client, "admin_w1").status_code == 200

        # admin 補打卡
        resp = client.post(
            "/api/attendance/record",
            json={
                "employee_id": target_id,
                "date": leave_date.isoformat(),
                "punch_in": "09:00",
                "punch_out": "18:00",
            },
        )
        assert resp.status_code in (200, 201), resp.text

        # 驗證 DB row 已正確關聯 leave_record_id
        with sf() as s:
            row = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == target_id,
                    Attendance.attendance_date == leave_date,
                )
                .first()
            )
            assert row is not None, "Attendance row 未寫入"
            assert (
                row.leave_record_id == leave_id
            ), f"leave_record_id 應為 {leave_id}，實際為 {row.leave_record_id}"

    def test_w2_repeated_edit_preserves_leave_record_id(self, att_client):
        """W-2: admin 重複編輯同一 row → leave_record_id 保留不被蓋掉。"""
        client, sf = att_client
        leave_date = date(2026, 5, 23)

        with sf() as s:
            _make_user(s, username="admin_w2", permissions=ATT_PERMS, employee_id=None)
            target = _make_employee(s, employee_id="E_W2", name="W2員工")
            lv = _approve_full_day_leave(s, target.id, leave_date)
            s.commit()
            target_id = target.id
            leave_id = lv.id

        assert _login(client, "admin_w2").status_code == 200

        # 第一次寫入
        resp1 = client.post(
            "/api/attendance/record",
            json={
                "employee_id": target_id,
                "date": leave_date.isoformat(),
                "punch_in": "09:00",
                "punch_out": "18:00",
            },
        )
        assert resp1.status_code in (200, 201), resp1.text

        # 第二次編輯（改時間）
        resp2 = client.post(
            "/api/attendance/record",
            json={
                "employee_id": target_id,
                "date": leave_date.isoformat(),
                "punch_in": "08:30",
                "punch_out": "17:30",
            },
        )
        assert resp2.status_code in (200, 201), resp2.text

        # 驗證 leave_record_id 仍保留
        with sf() as s:
            row = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == target_id,
                    Attendance.attendance_date == leave_date,
                )
                .first()
            )
            assert row is not None, "Attendance row 遺失"
            assert (
                row.leave_record_id == leave_id
            ), f"重複編輯後 leave_record_id 應保留 {leave_id}，實際為 {row.leave_record_id}"


# ══════════════════════════════════════════════════════════════════════
# TestExcelUploadWithLeave
# ══════════════════════════════════════════════════════════════════════


class TestExcelUploadWithLeave:
    def test_w3_upload_row_leave_aware(self, att_client):
        """W-3: approve 半天請假 09:00-13:00 → Excel upload 該日 punch_in=09:30
        → leave_record_id 對齊 / partial_leave_hours=4 / late=0
        (late-aware: 請假涵蓋 09:00-13:00，09:30 punch_in 在假期內 → late=0)

        測試策略:直接構造 Attendance 物件並呼叫 merge_attendance_with_leave,
        模擬 upload_attendance 每個 Excel row 的 build+merge 流程。
        """
        client, sf = att_client
        leave_date = date(2026, 5, 22)

        with sf() as s:
            target = _make_employee(s, employee_id="E_W3", name="W3員工")
            # 半天請假 09:00-13:00，4 小時
            lv = LeaveRecord(
                employee_id=target.id,
                leave_type="personal",
                start_date=leave_date,
                end_date=leave_date,
                leave_hours=4.0,
                start_time="09:00",
                end_time="13:00",
                is_approved=True,
            )
            s.add(lv)
            s.commit()
            target_id = target.id
            leave_id = lv.id

        # 模擬 upload_attendance 對 Excel row 構造的 Attendance（punch_in=09:30）
        # 上傳前 caller 算出 late（因為 09:30 > 09:00），但 merge 後應歸零
        with sf() as s:
            att = Attendance(
                employee_id=target_id,
                attendance_date=leave_date,
                punch_in_time=datetime(2026, 5, 22, 9, 30),
                punch_out_time=datetime(2026, 5, 22, 18, 0),
                status="late",
                is_late=True,
                is_early_leave=False,
                is_missing_punch_in=False,
                is_missing_punch_out=False,
                late_minutes=30,
                early_leave_minutes=0,
                remark="部門: 測試",
            )
            s.add(att)
            # 在 session.add 後、session.commit 前呼叫 merge（對齊 upload 流程）
            merge_attendance_with_leave(att, s)
            s.commit()

        # 驗證 merge 結果
        with sf() as s:
            row = (
                s.query(Attendance)
                .filter(
                    Attendance.employee_id == target_id,
                    Attendance.attendance_date == leave_date,
                )
                .first()
            )
            assert row is not None, "Attendance row 未寫入"
            assert (
                row.leave_record_id == leave_id
            ), f"leave_record_id 應為 {leave_id}，實際為 {row.leave_record_id}"
            assert row.partial_leave_hours == Decimal(
                "4.0"
            ), f"partial_leave_hours 應為 4.0，實際為 {row.partial_leave_hours}"
            assert (
                row.late_minutes == 0
            ), f"leave-aware 後 late_minutes 應為 0，實際為 {row.late_minutes}"
