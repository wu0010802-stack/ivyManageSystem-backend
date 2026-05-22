"""sync service unit tests U-1~U-15"""

import pytest
from datetime import date, time
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services import employee_leave_attendance_sync as sync
from models.base import Base
from models.attendance import Attendance, AttendanceStatus


class TestExceptions:
    def test_leave_attendance_conflict_is_exception(self):
        assert issubclass(sync.LeaveAttendanceConflict, Exception)

    def test_leave_not_approved_is_value_error(self):
        assert issubclass(sync.LeaveNotApproved, ValueError)

    def test_leave_partial_time_missing_is_value_error(self):
        assert issubclass(sync.LeavePartialTimeMissing, ValueError)


class TestIsFullDay:
    def test_full_day_when_no_times_and_hours_8(self):
        leave = make_leave(start_time=None, end_time=None, leave_hours=8.0)
        assert sync._is_full_day(leave) is True

    def test_full_day_when_no_times_and_hours_none(self):
        leave = make_leave(start_time=None, end_time=None, leave_hours=None)
        assert sync._is_full_day(leave) is True

    def test_not_full_day_when_start_time_set(self):
        leave = make_leave(start_time="09:00", end_time="12:00", leave_hours=4.0)
        assert sync._is_full_day(leave) is False

    def test_not_full_day_when_hours_lt_8(self):
        leave = make_leave(start_time="09:00", end_time="12:00", leave_hours=3.0)
        assert sync._is_full_day(leave) is False


class TestAssertLeaveTimeConsistent:
    def test_full_day_passes(self):
        leave = make_leave(start_time=None, end_time=None, leave_hours=8.0)
        sync._assert_leave_time_consistent(leave)  # 不該 raise

    def test_partial_with_times_passes(self):
        leave = make_leave(start_time="09:00", end_time="12:00", leave_hours=4.0)
        sync._assert_leave_time_consistent(leave)

    def test_partial_without_start_time_raises(self):
        leave = make_leave(start_time=None, end_time="12:00", leave_hours=4.0)
        with pytest.raises(sync.LeavePartialTimeMissing):
            sync._assert_leave_time_consistent(leave)

    def test_partial_without_end_time_raises(self):
        leave = make_leave(start_time="09:00", end_time=None, leave_hours=4.0)
        with pytest.raises(sync.LeavePartialTimeMissing):
            sync._assert_leave_time_consistent(leave)


# Helper:module-level stub,模擬 LeaveRecord 必要欄位
class _FakeLeave:
    """測試專用 stub,模擬 LeaveRecord 必要欄位。"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        self.employee_id = kwargs.get("employee_id", 1)
        self.start_date = kwargs.get("start_date", date(2026, 5, 22))
        self.end_date = kwargs.get("end_date", date(2026, 5, 22))
        self.start_time = kwargs.get("start_time")
        self.end_time = kwargs.get("end_time")
        self.leave_hours = kwargs.get("leave_hours", 8.0)
        self.leave_type = kwargs.get("leave_type", "personal")
        self.is_approved = kwargs.get("is_approved", True)


def make_leave(**kwargs):
    return _FakeLeave(**kwargs)


# ── SQLAlchemy 整合 fixture（U-1 / U-2）────────────────────────────


@pytest.fixture
def db_session(tmp_path):
    """In-memory SQLite session，建全套 schema。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def sample_employee(db_session):
    """建一個測試員工（僅填 nullable=False 且無 default 的欄位）。"""
    from models.employee import Employee

    emp = Employee(
        employee_id="T001",
        name="測試員工",
        base_salary=36000,
        is_active=True,
    )
    db_session.add(emp)
    db_session.commit()
    return emp


@pytest.fixture
def approved_full_day_leave(db_session, sample_employee):
    """5/22~5/24 全天 personal 假，已核可。"""
    from models.leave import LeaveRecord

    lv = LeaveRecord(
        employee_id=sample_employee.id,
        leave_type="personal",
        start_date=date(2026, 5, 22),
        end_date=date(2026, 5, 24),
        leave_hours=8.0,
        start_time=None,
        end_time=None,
        is_approved=True,
    )
    db_session.add(lv)
    db_session.commit()
    return lv


class TestApplyFullDay:
    def test_u1_apply_full_day_no_existing_attendance(
        self, db_session, approved_full_day_leave
    ):
        """U-1: apply 全天 3 天 + 無既有 attendance → 建 3 筆 status=LEAVE / punch=NULL"""
        written = sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()

        assert written == [date(2026, 5, 22), date(2026, 5, 23), date(2026, 5, 24)]

        rows = (
            db_session.query(Attendance)
            .filter_by(employee_id=approved_full_day_leave.employee_id)
            .order_by(Attendance.attendance_date)
            .all()
        )

        assert len(rows) == 3
        for row in rows:
            assert row.status == AttendanceStatus.LEAVE.value
            assert row.punch_in_time is None
            assert row.punch_out_time is None
            assert row.leave_record_id == approved_full_day_leave.id
            assert row.partial_leave_hours is None
            assert row.late_minutes == 0

    def test_u2_apply_full_day_overwrites_existing_absent(
        self, db_session, sample_employee, approved_full_day_leave
    ):
        """U-2: apply 全天請假 + 其中一天既有 ABSENT row → 更新為 LEAVE"""
        # 預先建 5/23 ABSENT row
        existing = Attendance(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 23),
            status=AttendanceStatus.ABSENT.value,
        )
        db_session.add(existing)
        db_session.commit()

        sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()

        row = (
            db_session.query(Attendance)
            .filter_by(
                employee_id=sample_employee.id,
                attendance_date=date(2026, 5, 23),
            )
            .first()
        )
        assert row.status == AttendanceStatus.LEAVE.value
        assert row.leave_record_id == approved_full_day_leave.id
