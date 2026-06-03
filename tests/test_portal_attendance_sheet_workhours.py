"""P2-1 回歸：portal 考勤表對 punch_in == punch_out 不可當跨夜班 +1 天。

原本 line 308 用 `effective_out <= effective_in` → 相等時也補一天，
虛增約 24 小時工時（拉高 avg_work_hours）。其他寫入路徑（補打卡/匯入/單筆建檔）
對「相等」一律 422 拒絕，唯獨此唯讀考勤表把相等當跨夜放行。改成嚴格小於 `<`。
"""

import os
import sys
from datetime import date, datetime, time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.base import Base
from models.database import Attendance, Employee
from api.portal.attendance import get_attendance_sheet


@pytest.fixture
def portal_db(tmp_path):
    db_path = tmp_path / "portal-att-sheet.sqlite"
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

    yield session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_equal_punch_in_out_not_counted_as_overnight(portal_db):
    """punch_in == punch_out（08:00）→ 該日 work_hours 不應虛增為約 24 小時。"""
    sf = portal_db
    with sf() as s:
        emp = Employee(
            employee_id="E_p21",
            name="工時測試",
            base_salary=30000,
            is_active=True,
        )
        s.add(emp)
        s.flush()
        # 同一天上下班時間相等（資料異常）
        s.add(
            Attendance(
                employee_id=emp.id,
                attendance_date=date(2026, 5, 15),
                punch_in_time=datetime.combine(date(2026, 5, 15), time(8, 0)),
                punch_out_time=datetime.combine(date(2026, 5, 15), time(8, 0)),
            )
        )
        s.commit()
        emp_id = emp.id

    sheet = get_attendance_sheet(
        year=2026, month=5, current_user={"employee_id": emp_id}
    )

    day_row = next(r for r in sheet["days"] if r["date"] == "2026-05-15")
    # 相等 punch 不應被當跨夜班補一天 → work_hours 不該接近 24
    assert (day_row["work_hours"] or 0) < 1
    assert sheet["summary"]["avg_work_hours"] < 1
