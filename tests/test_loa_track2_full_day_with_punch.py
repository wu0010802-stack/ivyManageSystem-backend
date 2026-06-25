"""F-A 回歸：全日請假當天有打卡時，sync 與 merge 兩寫入路徑扣款必須一致。

業主裁定語意：全日假當天若有真實打卡 → 視同銷假/正常上班（員工實際有來上班）。
- 保留打卡（不可清 punch）
- partial_leave_hours = 0
- status 依打卡重算（NORMAL/LATE/EARLY_LEAVE）
- leave_record_id 仍連結（追溯用，但 0 扣款）

兩寫入順序 parity：
  (a) 先核假（sync.apply）後寫打卡（merge_attendance_with_leave）
  (b) 先打卡後核假（sync.apply）
兩順序後 status / partial_leave_hours 一致、薪資請假扣款一致、真實 punch 未被清。
"""

from datetime import date, time, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services import employee_leave_attendance_sync as sync
from utils.attendance_leave_merge import merge_attendance_with_leave
from models.base import Base
from models.attendance import Attendance, AttendanceStatus


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def sample_employee(db_session):
    from models.employee import Employee

    emp = Employee(
        employee_id="T001",
        name="測試員工",
        base_salary=36000,
        is_active=True,
        work_start_time="09:00",
        work_end_time="18:00",
    )
    db_session.add(emp)
    db_session.commit()
    return emp


@pytest.fixture
def approved_full_day_leave(db_session, sample_employee):
    """5/22 單日全天 personal 假，已核可。"""
    from models.leave import LeaveRecord

    lv = LeaveRecord(
        employee_id=sample_employee.id,
        leave_type="personal",
        start_date=date(2026, 5, 22),
        end_date=date(2026, 5, 22),
        leave_hours=8.0,
        start_time=None,
        end_time=None,
        status="approved",
    )
    db_session.add(lv)
    db_session.commit()
    return lv


def _make_punched_row(emp_id, d):
    """09:00-18:00 正常打卡的 Attendance row（尚未經 sync/merge）。"""
    return Attendance(
        employee_id=emp_id,
        attendance_date=d,
        status=AttendanceStatus.NORMAL.value,
        punch_in_time=datetime.combine(d, time(9, 0)),
        punch_out_time=datetime.combine(d, time(18, 0)),
    )


def _leave_deduction(att, lv):
    """以 Attendance 為 SoT 算單列請假扣款（引擎口徑）。"""
    from services.salary.utils import _sum_leave_deduction

    return _sum_leave_deduction([(att, lv)], daily_salary=1200.0)


class TestFullDayLeaveWithPunchParity:
    def test_order_b_punch_then_leave_does_not_destroy_punch(
        self, db_session, sample_employee, approved_full_day_leave
    ):
        """順序 (b)：先打卡後核假（sync.apply）→ 視同銷假，保留打卡、partial=0、0 扣款。"""
        row = _make_punched_row(sample_employee.id, date(2026, 5, 22))
        db_session.add(row)
        db_session.commit()

        sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()

        row = (
            db_session.query(Attendance)
            .filter_by(
                employee_id=sample_employee.id, attendance_date=date(2026, 5, 22)
            )
            .first()
        )
        # 真實打卡未被清除
        assert row.punch_in_time is not None
        assert row.punch_in_time.time() == time(9, 0)
        assert row.punch_out_time is not None
        assert row.punch_out_time.time() == time(18, 0)
        # 視同銷假：partial_leave_hours = 0，非 None、非 8
        assert row.partial_leave_hours == Decimal("0")
        # status 非 LEAVE（視同正常上班）
        assert row.status != AttendanceStatus.LEAVE.value
        # leave_record_id 仍連結（追溯用）
        assert row.leave_record_id == approved_full_day_leave.id
        # 薪資請假扣款 = 0
        assert _leave_deduction(row, approved_full_day_leave) == 0.0

    def test_order_a_and_b_parity(
        self, db_session, sample_employee, approved_full_day_leave
    ):
        """順序 (a) merge 與順序 (b) sync 結果一致（status/partial/扣款）。"""
        # 順序 (b)：先打卡後核假 → sync.apply
        row_b = _make_punched_row(sample_employee.id, date(2026, 5, 22))
        db_session.add(row_b)
        db_session.commit()
        sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()
        row_b = (
            db_session.query(Attendance)
            .filter_by(
                employee_id=sample_employee.id, attendance_date=date(2026, 5, 22)
            )
            .first()
        )
        b_status = row_b.status
        b_partial = row_b.partial_leave_hours
        b_ded = _leave_deduction(row_b, approved_full_day_leave)

        # 順序 (a)：模擬「先核假再寫打卡」→ 用 merge 算（不依賴 sync 寫入的 row）
        row_a = _make_punched_row(sample_employee.id, date(2026, 5, 22))
        merge_attendance_with_leave(row_a, db_session)
        a_status = row_a.status
        a_partial = row_a.partial_leave_hours
        a_ded = _leave_deduction(row_a, approved_full_day_leave)

        assert a_status == b_status
        assert (a_partial or Decimal("0")) == (b_partial or Decimal("0"))
        assert a_ded == b_ded == 0.0
