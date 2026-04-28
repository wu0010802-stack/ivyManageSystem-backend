"""上游事件 → SalaryRecord.needs_recalc 傳播回歸測試。

涵蓋:
- P1.2: update_leave / delete_leave 重算失敗 → 標 stale
- P1.3: batch_approve_leaves 重算失敗 → 標 stale
- P1.4: 會議 create / update / delete → 標 stale
- P1.5: 排班 upsert / delete 已封存月 → 409;未封存月 → 標 stale
- P1.6: 假日匯入 封存月 → 409;force=true 通過;未封存月整月標 stale
- P1.1: 考勤 CSV 匯入 → 標 stale
- P2.7: LINE bot 「我的薪資」只回封存且非 stale 的記錄
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import api.salary as salary_module
import api.leaves as leaves_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.salary import router as salary_router
from api.leaves import router as leaves_router
from api.meetings import router as meetings_router
from api.shifts import router as shifts_router
from api.events import router as events_router
from api.attendance import router as attendance_router
from models.database import (
    Base,
    Employee,
    User,
    SalaryRecord,
    LeaveRecord,
    MeetingRecord,
    ShiftType,
    DailyShift,
    Holiday,
    Attendance,
)
from utils.auth import hash_password


@pytest.fixture
def stale_client(tmp_path):
    db_path = tmp_path / "stale-prop.sqlite"
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

    salary_module.init_salary_services(MagicMock(), MagicMock())
    salary_module._snapshot_lazy_guard.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(salary_router)
    app.include_router(leaves_router)
    app.include_router(meetings_router)
    app.include_router(shifts_router)
    app.include_router(events_router)
    app.include_router(attendance_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _admin_login(client, sf, username="admin", password="AdminPass123"):
    with sf() as session:
        session.add(
            User(
                employee_id=None,
                username=username,
                password_hash=hash_password(password),
                role="admin",
                permissions=-1,
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text


def _seed_employee(sf, name: str, employee_id_str: str = None) -> int:
    with sf() as session:
        emp = Employee(
            employee_id=employee_id_str or f"E_{name}",
            name=name,
            base_salary=30000,
            employee_type="regular",
            is_active=True,
            hire_date=date(2025, 1, 1),
        )
        session.add(emp)
        session.commit()
        return emp.id


def _seed_salary_record(
    sf,
    emp_id: int,
    year: int = 2026,
    month: int = 3,
    needs_recalc: bool = False,
    is_finalized: bool = False,
):
    with sf() as session:
        rec = SalaryRecord(
            employee_id=emp_id,
            salary_year=year,
            salary_month=month,
            base_salary=30000,
            gross_salary=30000,
            net_salary=28000,
            total_deduction=2000,
            needs_recalc=needs_recalc,
            is_finalized=is_finalized,
        )
        session.add(rec)
        session.commit()
        return rec.id


def _get_record(sf, emp_id: int, year=2026, month=3):
    with sf() as session:
        return (
            session.query(SalaryRecord)
            .filter_by(employee_id=emp_id, salary_year=year, salary_month=month)
            .first()
        )


# ─────────────────────────────────────────────────────────────────────────────
# P1.2 update_leave / delete_leave 重算失敗 → 標 stale
# ─────────────────────────────────────────────────────────────────────────────


def _seed_approved_leave(sf, emp_id: int) -> int:
    with sf() as session:
        leave = LeaveRecord(
            employee_id=emp_id,
            leave_type="事假",
            start_date=datetime(2026, 3, 10),
            end_date=datetime(2026, 3, 10),
            leave_hours=8,
            reason="test",
            is_approved=True,
            approved_by="admin",
        )
        session.add(leave)
        session.commit()
        return leave.id


class TestLeaveUpdateRecalcFailureMarksStale:
    def test_update_leave_recalc_exception_marks_stale(self, stale_client):
        client, sf = stale_client
        emp_id = _seed_employee(sf, "員工A", "A001")
        _seed_salary_record(sf, emp_id, needs_recalc=False)
        leave_id = _seed_approved_leave(sf, emp_id)
        _admin_login(client, sf)

        broken_engine = MagicMock()
        broken_engine.process_salary_calculation.side_effect = RuntimeError("模擬")
        old = leaves_module._salary_engine
        leaves_module._salary_engine = broken_engine
        try:
            res = client.put(
                f"/api/leaves/{leave_id}",
                json={"reason": "改一下"},
            )
        finally:
            leaves_module._salary_engine = old

        assert res.status_code == 200, res.text
        assert "salary_warning" in res.json()
        rec = _get_record(sf, emp_id)
        assert rec.needs_recalc is True


class TestLeaveDeleteRecalcFailureMarksStale:
    def test_delete_leave_recalc_exception_marks_stale(self, stale_client):
        client, sf = stale_client
        emp_id = _seed_employee(sf, "員工A", "A001")
        _seed_salary_record(sf, emp_id, needs_recalc=False)
        leave_id = _seed_approved_leave(sf, emp_id)
        _admin_login(client, sf)

        broken_engine = MagicMock()
        broken_engine.process_salary_calculation.side_effect = RuntimeError("模擬")
        old = leaves_module._salary_engine
        leaves_module._salary_engine = broken_engine
        try:
            res = client.delete(f"/api/leaves/{leave_id}")
        finally:
            leaves_module._salary_engine = old

        assert res.status_code == 200, res.text
        assert "salary_warning" in res.json()
        rec = _get_record(sf, emp_id)
        assert rec.needs_recalc is True


class TestBatchApproveLeaveRecalcFailureMarksStale:
    def test_batch_approve_recalc_exception_marks_stale(self, stale_client):
        client, sf = stale_client
        emp_id = _seed_employee(sf, "員工A", "A001")
        _seed_salary_record(sf, emp_id, needs_recalc=False)
        # 待審假單
        with sf() as session:
            leave = LeaveRecord(
                employee_id=emp_id,
                leave_type="事假",
                start_date=datetime(2026, 3, 10),
                end_date=datetime(2026, 3, 10),
                leave_hours=8,
                reason="test",
                is_approved=None,
            )
            session.add(leave)
            session.commit()
            leave_id = leave.id
        _admin_login(client, sf)

        broken_engine = MagicMock()
        broken_engine.process_salary_calculation.side_effect = RuntimeError("模擬")
        old = leaves_module._salary_engine
        leaves_module._salary_engine = broken_engine
        try:
            res = client.post(
                "/api/leaves/batch-approve",
                json={"ids": [leave_id], "approved": True},
            )
        finally:
            leaves_module._salary_engine = old

        assert res.status_code == 200, res.text
        rec = _get_record(sf, emp_id)
        assert rec.needs_recalc is True


# ─────────────────────────────────────────────────────────────────────────────
# P1.4 會議 CRUD → 標 stale
# ─────────────────────────────────────────────────────────────────────────────


class TestMeetingCRUDMarksStale:
    def test_create_meeting_marks_stale(self, stale_client):
        client, sf = stale_client
        emp_id = _seed_employee(sf, "員工A", "A001")
        _seed_salary_record(sf, emp_id, needs_recalc=False)
        _admin_login(client, sf)

        res = client.post(
            "/api/meetings",
            json={
                "employee_id": emp_id,
                "meeting_date": "2026-03-15",
                "attended": True,
                "overtime_hours": 1.0,
            },
        )
        assert res.status_code == 201, res.text
        rec = _get_record(sf, emp_id)
        assert rec.needs_recalc is True

    def test_update_meeting_marks_stale(self, stale_client):
        client, sf = stale_client
        emp_id = _seed_employee(sf, "員工A", "A001")
        _seed_salary_record(sf, emp_id, needs_recalc=False)
        with sf() as session:
            mr = MeetingRecord(
                employee_id=emp_id,
                meeting_date=date(2026, 3, 15),
                meeting_type="staff_meeting",
                attended=True,
                overtime_hours=1,
                overtime_pay=200,
            )
            session.add(mr)
            session.commit()
            mr_id = mr.id
        _admin_login(client, sf)

        res = client.put(
            f"/api/meetings/{mr_id}",
            json={"attended": False},
        )
        assert res.status_code == 200, res.text
        rec = _get_record(sf, emp_id)
        assert rec.needs_recalc is True

    def test_delete_meeting_marks_stale(self, stale_client):
        client, sf = stale_client
        emp_id = _seed_employee(sf, "員工A", "A001")
        _seed_salary_record(sf, emp_id, needs_recalc=False)
        with sf() as session:
            mr = MeetingRecord(
                employee_id=emp_id,
                meeting_date=date(2026, 3, 15),
                meeting_type="staff_meeting",
                attended=True,
                overtime_hours=1,
                overtime_pay=200,
            )
            session.add(mr)
            session.commit()
            mr_id = mr.id
        _admin_login(client, sf)

        res = client.delete(f"/api/meetings/{mr_id}")
        assert res.status_code == 200, res.text
        rec = _get_record(sf, emp_id)
        assert rec.needs_recalc is True


# ─────────────────────────────────────────────────────────────────────────────
# P1.5 排班 upsert/delete 封存月 → 409;未封存月 → 標 stale
# ─────────────────────────────────────────────────────────────────────────────


def _seed_shift_type(sf):
    with sf() as session:
        st = ShiftType(
            name="早班",
            work_start="08:00",
            work_end="17:00",
            is_active=True,
        )
        session.add(st)
        session.commit()
        return st.id


class TestShiftUpsertFinalizedGuardAndStale:
    def test_upsert_blocked_when_month_finalized(self, stale_client):
        client, sf = stale_client
        emp_id = _seed_employee(sf, "員工A", "A001")
        _seed_salary_record(sf, emp_id, is_finalized=True)
        st_id = _seed_shift_type(sf)
        _admin_login(client, sf)

        res = client.post(
            "/api/shifts/daily",
            json={
                "employee_id": emp_id,
                "date": "2026-03-15",
                "shift_type_id": st_id,
            },
        )
        assert res.status_code == 409, res.text

    def test_upsert_marks_stale_when_not_finalized(self, stale_client):
        client, sf = stale_client
        emp_id = _seed_employee(sf, "員工A", "A001")
        _seed_salary_record(sf, emp_id, needs_recalc=False, is_finalized=False)
        st_id = _seed_shift_type(sf)
        _admin_login(client, sf)

        res = client.post(
            "/api/shifts/daily",
            json={
                "employee_id": emp_id,
                "date": "2026-03-15",
                "shift_type_id": st_id,
            },
        )
        assert res.status_code == 201, res.text
        rec = _get_record(sf, emp_id)
        assert rec.needs_recalc is True

    def test_delete_blocked_when_month_finalized(self, stale_client):
        client, sf = stale_client
        emp_id = _seed_employee(sf, "員工A", "A001")
        _seed_salary_record(sf, emp_id, is_finalized=True)
        st_id = _seed_shift_type(sf)
        with sf() as session:
            ds = DailyShift(
                employee_id=emp_id, shift_type_id=st_id, date=date(2026, 3, 15)
            )
            session.add(ds)
            session.commit()
            ds_id = ds.id
        _admin_login(client, sf)

        res = client.delete(f"/api/shifts/daily/{ds_id}")
        assert res.status_code == 409, res.text

    def test_delete_marks_stale_when_not_finalized(self, stale_client):
        client, sf = stale_client
        emp_id = _seed_employee(sf, "員工A", "A001")
        _seed_salary_record(sf, emp_id, needs_recalc=False, is_finalized=False)
        st_id = _seed_shift_type(sf)
        with sf() as session:
            ds = DailyShift(
                employee_id=emp_id, shift_type_id=st_id, date=date(2026, 3, 15)
            )
            session.add(ds)
            session.commit()
            ds_id = ds.id
        _admin_login(client, sf)

        res = client.delete(f"/api/shifts/daily/{ds_id}")
        assert res.status_code == 200, res.text
        rec = _get_record(sf, emp_id)
        assert rec.needs_recalc is True


# ─────────────────────────────────────────────────────────────────────────────
# P1.6 假日匯入封存月 → 409;force 通過;未封存月整月標 stale
# ─────────────────────────────────────────────────────────────────────────────


def _build_holiday_xlsx(rows: list[tuple[str, str, str]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["日期", "假日名稱", "說明(可空)"])
    for d, n, desc in rows:
        ws.append([d, n, desc])
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


class TestHolidayImportFinalizedGuardAndStale:
    def test_import_blocked_when_month_finalized(self, stale_client):
        client, sf = stale_client
        emp_id = _seed_employee(sf, "員工A", "A001")
        _seed_salary_record(sf, emp_id, is_finalized=True)
        _admin_login(client, sf)

        content = _build_holiday_xlsx([("2026-03-20", "測試假日", "")])
        res = client.post(
            "/api/events/holidays/import",
            files={
                "file": (
                    "holidays.xlsx",
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert res.status_code == 409, res.text
        with sf() as session:
            assert session.query(Holiday).count() == 0

    def test_import_force_bypasses_with_reason(self, stale_client):
        client, sf = stale_client
        emp_id = _seed_employee(sf, "員工A", "A001")
        _seed_salary_record(sf, emp_id, is_finalized=True)
        _admin_login(client, sf)

        content = _build_holiday_xlsx([("2026-03-20", "測試假日", "")])
        res = client.post(
            "/api/events/holidays/import?force=true&force_reason=政府公告補登假日記錄",
            files={
                "file": (
                    "holidays.xlsx",
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert res.status_code == 200, res.text
        assert res.json()["upserted"] == 1

    def test_import_force_requires_reason(self, stale_client):
        client, sf = stale_client
        _admin_login(client, sf)

        content = _build_holiday_xlsx([("2026-03-20", "測試假日", "")])
        res = client.post(
            "/api/events/holidays/import?force=true",
            files={
                "file": (
                    "holidays.xlsx",
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert res.status_code == 400, res.text

    def test_import_marks_all_unfinalized_records_in_month_stale(self, stale_client):
        client, sf = stale_client
        emp_a = _seed_employee(sf, "員工A", "A001")
        emp_b = _seed_employee(sf, "員工B", "B001")
        # 兩位員工 3 月薪資都未封存
        _seed_salary_record(sf, emp_a, year=2026, month=3, needs_recalc=False)
        _seed_salary_record(sf, emp_b, year=2026, month=3, needs_recalc=False)
        # 另一員工的 4 月薪資不該被影響
        _seed_salary_record(sf, emp_a, year=2026, month=4, needs_recalc=False)
        _admin_login(client, sf)

        content = _build_holiday_xlsx([("2026-03-25", "測試假日", "")])
        res = client.post(
            "/api/events/holidays/import",
            files={
                "file": (
                    "holidays.xlsx",
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert res.status_code == 200, res.text

        rec_a3 = _get_record(sf, emp_a, year=2026, month=3)
        rec_b3 = _get_record(sf, emp_b, year=2026, month=3)
        rec_a4 = _get_record(sf, emp_a, year=2026, month=4)
        assert rec_a3.needs_recalc is True
        assert rec_b3.needs_recalc is True
        assert rec_a4.needs_recalc is False  # 4 月不在影響範圍


# ─────────────────────────────────────────────────────────────────────────────
# P1.1 考勤 CSV 匯入 → 標 stale
# ─────────────────────────────────────────────────────────────────────────────


class TestAttendanceCSVUploadMarksStale:
    def test_csv_upload_marks_affected_months_stale(self, stale_client):
        client, sf = stale_client
        emp_id = _seed_employee(sf, "員工A", "A001")
        _seed_salary_record(sf, emp_id, year=2026, month=3, needs_recalc=False)
        _admin_login(client, sf)

        res = client.post(
            "/api/attendance/upload-csv",
            json={
                "year": 2026,
                "month": 3,
                "records": [
                    {
                        "employee_number": "A001",
                        "name": "員工A",
                        "department": "test",
                        "date": "2026-03-15",
                        "weekday": "Sun",
                        "punch_in": "08:00",
                        "punch_out": "17:00",
                    }
                ],
            },
        )
        assert res.status_code == 200, res.text
        rec = _get_record(sf, emp_id, year=2026, month=3)
        assert rec.needs_recalc is True


# ─────────────────────────────────────────────────────────────────────────────
# P2.7 LINE bot 「我的薪資」只回封存且非 stale
# ─────────────────────────────────────────────────────────────────────────────


class TestLineBotMySalaryFiltersDraft:
    """單元測試 LineService._handle_text_command 對「我的薪資」的過濾邏輯。"""

    def _setup(self, sf, *, finalized: bool, needs_recalc: bool):
        emp_id = _seed_employee(sf, "員工A", "A001")
        with sf() as session:
            user = User(
                employee_id=emp_id,
                username="user_a",
                password_hash=hash_password("Pw1234567890"),
                role="teacher",
                permissions=0,
                is_active=True,
                line_user_id="LINE_X",
                must_change_password=False,
            )
            session.add(user)
            session.commit()
        _seed_salary_record(
            sf,
            emp_id,
            year=2026,
            month=3,
            needs_recalc=needs_recalc,
            is_finalized=finalized,
        )
        return emp_id

    def test_draft_only_returns_no_record_message(self, stale_client):
        _, sf = stale_client
        self._setup(sf, finalized=False, needs_recalc=False)

        from services.line_service import LineService

        svc = LineService()
        captured = {}

        def fake_reply(reply_token, text):
            captured["text"] = text

        svc._reply = fake_reply
        with sf() as session:
            svc.handle_webhook_message("LINE_X", "我的薪資", "REPLY", session)
        assert "查無已結算" in captured["text"]

    def test_stale_finalized_record_not_returned(self, stale_client):
        _, sf = stale_client
        self._setup(sf, finalized=True, needs_recalc=True)

        from services.line_service import LineService

        svc = LineService()
        captured = {}
        svc._reply = lambda rt, t: captured.update(text=t)
        with sf() as session:
            svc.handle_webhook_message("LINE_X", "我的薪資", "REPLY", session)
        assert "查無已結算" in captured["text"]

    def test_finalized_non_stale_record_returned(self, stale_client):
        _, sf = stale_client
        self._setup(sf, finalized=True, needs_recalc=False)

        from services.line_service import LineService

        svc = LineService()
        captured = {}
        svc._reply = lambda rt, t: captured.update(text=t)
        with sf() as session:
            svc.handle_webhook_message("LINE_X", "我的薪資", "REPLY", session)
        assert "薪資摘要" in captured["text"]
