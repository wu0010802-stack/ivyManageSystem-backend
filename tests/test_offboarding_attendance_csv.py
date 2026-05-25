"""驗證離職員工過去 12 月 attendance CSV 匯出（§Task 4）。"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.attendance import Attendance, AttendanceStatus, Base as AttBase
from models.employee import Employee, Base as EmpBase

from services.offboarding.attendance_csv import generate_attendance_csv

_counter = 0


@pytest.fixture
def db_session(tmp_path):
    """SQLite test session（對齊既有 offboarding test pattern）。"""
    db_path = tmp_path / "attendance_csv.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    EmpBase.metadata.create_all(engine)
    AttBase.metadata.create_all(engine)

    session = session_factory()
    yield session
    session.close()

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def employee_factory(db_session):
    """建立測試員工。"""

    def _factory(
        *,
        name: str = "測試員工",
        hire_date: date = date(2020, 1, 1),
    ) -> Employee:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"ACSV{_counter:04d}",
            name=name,
            id_number=f"A{_counter:09d}",
            hire_date=hire_date,
            position="教保員",
            is_active=True,
        )
        db_session.add(emp)
        db_session.flush()
        return emp

    return _factory


@pytest.fixture
def attendance_factory(db_session):
    """建立考勤記錄。"""

    def _factory(
        *,
        employee_id: int,
        attendance_date: date,
        status: str = AttendanceStatus.NORMAL.value,
        punch_in_time: datetime | None = None,
        punch_out_time: datetime | None = None,
        is_late: bool = False,
        late_minutes: int = 0,
    ) -> Attendance:
        rec = Attendance(
            employee_id=employee_id,
            attendance_date=attendance_date,
            punch_in_time=punch_in_time
            or datetime(
                attendance_date.year, attendance_date.month, attendance_date.day, 8, 0
            ),
            punch_out_time=punch_out_time
            or datetime(
                attendance_date.year, attendance_date.month, attendance_date.day, 17, 0
            ),
            status=status,
            is_late=is_late,
            late_minutes=late_minutes,
        )
        db_session.add(rec)
        db_session.flush()
        return rec

    return _factory


def test_generates_csv_with_header_and_rows(
    db_session, employee_factory, attendance_factory
):
    """正常情境：有考勤資料，CSV 含標頭與資料列。"""
    emp = employee_factory(hire_date=date(2025, 1, 1))
    attendance_factory(employee_id=emp.id, attendance_date=date(2026, 5, 1))
    attendance_factory(employee_id=emp.id, attendance_date=date(2026, 5, 2))

    csv_bytes = generate_attendance_csv(
        db_session, emp.id, resign_date=date(2026, 6, 15)
    )

    assert isinstance(csv_bytes, bytes)
    # UTF-8 BOM
    assert csv_bytes[:3] == b"\xef\xbb\xbf"

    text = csv_bytes.decode("utf-8-sig")
    lines = [line for line in text.strip().split("\n") if line.strip()]
    # 標頭列
    assert len(lines) >= 3  # header + 2 data rows
    header = lines[0]
    assert "attendance_date" in header or "日期" in header
    # 確認資料存在
    assert "2026-05-01" in text
    assert "2026-05-02" in text


def test_empty_when_no_attendance(db_session, employee_factory):
    """無考勤資料仍回標頭列（讓員工確認無紀錄）。"""
    emp = employee_factory(hire_date=date(2025, 1, 1))

    csv_bytes = generate_attendance_csv(
        db_session, emp.id, resign_date=date(2026, 6, 15)
    )
    text = csv_bytes.decode("utf-8-sig")
    lines = [line for line in text.strip().split("\n") if line.strip()]
    assert len(lines) == 1  # 只有 header
    assert "attendance_date" in lines[0] or "日期" in lines[0]


def test_excludes_records_before_hire_date(
    db_session, employee_factory, attendance_factory
):
    """到職日前的考勤不應出現（解決 resign - 365 天早於 hire_date 時的邊界）。"""
    emp = employee_factory(hire_date=date(2026, 3, 1))
    # hire_date 前的記錄（不應被查進 CSV）
    attendance_factory(employee_id=emp.id, attendance_date=date(2025, 12, 1))
    # hire_date 後的記錄（應出現）
    attendance_factory(employee_id=emp.id, attendance_date=date(2026, 3, 15))

    csv_bytes = generate_attendance_csv(
        db_session, emp.id, resign_date=date(2026, 6, 15)
    )
    text = csv_bytes.decode("utf-8-sig")

    assert "2025-12-01" not in text  # hire 前不含
    assert "2026-03-15" in text  # hire 後含
