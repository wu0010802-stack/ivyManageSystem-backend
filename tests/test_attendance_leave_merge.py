"""M-1~M-7: merge_attendance_with_leave 純函式單元測試"""

import pytest
from datetime import date, time, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.attendance import Attendance, AttendanceStatus
from models.base import Base
from utils.attendance_leave_merge import merge_attendance_with_leave


@pytest.fixture
def db_session():
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


def _make_leave(db_session, emp_id, **kwargs):
    from models.leave import LeaveRecord

    lv = LeaveRecord(
        employee_id=emp_id,
        leave_type=kwargs.get("leave_type", "personal"),
        start_date=kwargs.get("start_date", date(2026, 5, 22)),
        end_date=kwargs.get("end_date", date(2026, 5, 22)),
        start_time=kwargs.get("start_time"),
        end_time=kwargs.get("end_time"),
        leave_hours=kwargs.get("leave_hours", 8.0),
        is_approved=kwargs.get("is_approved", True),
    )
    db_session.add(lv)
    db_session.commit()
    return lv


class TestMergeAttendanceWithLeave:
    def test_m1_no_leave_noop(self, db_session, sample_employee):
        """M-1: 當日無 approved leave → att.leave_record_id=None,att 本身不變"""
        att = Attendance(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.NORMAL.value,
            punch_in_time=datetime.combine(date(2026, 5, 22), time(9, 0)),
        )
        merge_attendance_with_leave(att, db_session)
        assert att.leave_record_id is None
        assert att.partial_leave_hours is None
        assert att.status == AttendanceStatus.NORMAL.value

    def test_m2_full_day_leave_no_punch(self, db_session, sample_employee):
        """M-2: 全天 leave + 無打卡 → status=LEAVE,清打卡"""
        lv = _make_leave(db_session, sample_employee.id, leave_hours=8.0)
        att = Attendance(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.ABSENT.value,  # 預設 ABSENT,merge 後變 LEAVE
        )
        merge_attendance_with_leave(att, db_session)
        assert att.status == AttendanceStatus.LEAVE.value
        assert att.leave_record_id == lv.id
        assert att.partial_leave_hours is None
        assert att.punch_in_time is None
        assert att.late_minutes == 0

    def test_m3_full_day_leave_with_punch(self, db_session, sample_employee):
        """M-3: 全天 leave + 有打卡(臨時上班)→ leave_record_id 寫入,status 保留,partial=0"""
        lv = _make_leave(db_session, sample_employee.id, leave_hours=8.0)
        att = Attendance(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.NORMAL.value,
            punch_in_time=datetime.combine(date(2026, 5, 22), time(9, 0)),
            punch_out_time=datetime.combine(date(2026, 5, 22), time(18, 0)),
            late_minutes=0,
        )
        merge_attendance_with_leave(att, db_session)
        assert att.leave_record_id == lv.id
        assert att.partial_leave_hours == Decimal("0")
        assert att.status == AttendanceStatus.NORMAL.value  # 保留 caller 算的
        assert att.punch_in_time == datetime.combine(date(2026, 5, 22), time(9, 0))

    def test_m4_partial_leave_with_punch(self, db_session, sample_employee):
        """M-4: 部分 leave + 有打卡 → partial_leave_hours,late_minutes leave-aware 重算"""
        lv = _make_leave(
            db_session,
            sample_employee.id,
            leave_hours=4.0,
            start_time="09:00",
            end_time="13:00",
        )
        att = Attendance(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.LATE.value,
            punch_in_time=datetime.combine(date(2026, 5, 22), time(9, 30)),
            late_minutes=30,  # caller 原本算的
        )
        merge_attendance_with_leave(att, db_session)
        assert att.leave_record_id == lv.id
        assert att.partial_leave_hours == Decimal("4.00")
        # 請假 09:00-13:00 涵蓋 09:30 → late=0
        assert att.late_minutes == 0
        # 因 late=0,status 退回 NORMAL
        assert att.status == AttendanceStatus.NORMAL.value

    def test_m5_partial_leave_no_punch(self, db_session, sample_employee):
        """M-5: 部分 leave + 無打卡 → status=ABSENT"""
        lv = _make_leave(
            db_session,
            sample_employee.id,
            leave_hours=4.0,
            start_time="09:00",
            end_time="13:00",
        )
        att = Attendance(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.NORMAL.value,  # caller 預設值
        )
        merge_attendance_with_leave(att, db_session)
        assert att.status == AttendanceStatus.ABSENT.value
        assert att.leave_record_id == lv.id
        assert att.partial_leave_hours == Decimal("4.00")

    def test_m6_multiple_leaves_uses_earliest(self, db_session, sample_employee):
        """M-6: 同日多筆 approved leave(異常)→ 取最早 id"""
        lv1 = _make_leave(
            db_session,
            sample_employee.id,
            leave_hours=4.0,
            start_time="09:00",
            end_time="13:00",
        )
        lv2 = _make_leave(
            db_session,
            sample_employee.id,
            leave_hours=2.0,
            start_time="14:00",
            end_time="16:00",
        )
        att = Attendance(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.NORMAL.value,
        )
        merge_attendance_with_leave(att, db_session)
        assert att.leave_record_id == lv1.id  # 取較早 id

    def test_m7_session_only_read(self, db_session, sample_employee):
        """M-7: helper 不修改 session(只讀),att 不加入 session dirty"""
        _make_leave(db_session, sample_employee.id, leave_hours=8.0)
        att = Attendance(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.NORMAL.value,
        )
        # 確保 att 未被 session 追蹤(transient 物件)
        merge_attendance_with_leave(att, db_session)
        # att 不應被加入 session.new(transient 物件,merge 只改其屬性)
        assert att not in db_session.new
        # session 不應有 dirty(merge 只讀 session)
        assert len(db_session.dirty) == 0
