"""sync service unit tests U-1~U-15"""

import pytest
from datetime import date, time, datetime
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


# ── Task 7 fixtures ───────────────────────────────────────────────


@pytest.fixture
def approved_partial_morning_leave(db_session, sample_employee):
    """5/22 半天 09:00-13:00(personal)，已核可"""
    from models.leave import LeaveRecord

    lv = LeaveRecord(
        employee_id=sample_employee.id,
        leave_type="personal",
        start_date=date(2026, 5, 22),
        end_date=date(2026, 5, 22),
        leave_hours=4.0,
        start_time="09:00",
        end_time="13:00",
        is_approved=True,
    )
    db_session.add(lv)
    db_session.commit()
    return lv


@pytest.fixture
def approved_partial_hour_leave(db_session, sample_employee):
    """5/22 小時假 1.5hr 09:00-10:30，已核可"""
    from models.leave import LeaveRecord

    lv = LeaveRecord(
        employee_id=sample_employee.id,
        leave_type="personal",
        start_date=date(2026, 5, 22),
        end_date=date(2026, 5, 22),
        leave_hours=1.5,
        start_time="09:00",
        end_time="10:30",
        is_approved=True,
    )
    db_session.add(lv)
    db_session.commit()
    return lv


class TestApplyPartial:
    def test_u3_apply_partial_with_existing_late_row(
        self, db_session, sample_employee, approved_partial_morning_leave
    ):
        """U-3: apply 半天 + 既有 LATE row → status 保持 LATE / partial_leave_hours=4 / late_minutes 重算"""
        existing = Attendance(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.LATE.value,
            punch_in_time=datetime.combine(date(2026, 5, 22), time(9, 30)),
            late_minutes=30,
        )
        db_session.add(existing)
        db_session.commit()

        sync.apply(db_session, approved_partial_morning_leave.id)
        db_session.flush()

        row = (
            db_session.query(Attendance)
            .filter_by(
                employee_id=sample_employee.id,
                attendance_date=date(2026, 5, 22),
            )
            .first()
        )
        # punch 保留
        assert row.punch_in_time is not None
        assert row.punch_in_time.time() == time(9, 30)
        # leave_record_id + partial_leave_hours 寫入
        assert row.leave_record_id == approved_partial_morning_leave.id
        assert row.partial_leave_hours == Decimal("4.00")
        # late_minutes 重算：scheduled_start=09:00，請假 09:00-13:00 涵蓋 → late=0
        assert row.late_minutes == 0
        # status 退回 NORMAL（原 LATE，late_minutes 歸零後）
        assert row.status == AttendanceStatus.NORMAL.value

    def test_u4_apply_partial_no_punch_becomes_absent(
        self, db_session, sample_employee, approved_partial_morning_leave
    ):
        """U-4: apply 半天 + 無 punch_in/out → status=ABSENT / partial_leave_hours=4"""
        sync.apply(db_session, approved_partial_morning_leave.id)
        db_session.flush()

        row = (
            db_session.query(Attendance)
            .filter_by(
                employee_id=sample_employee.id,
                attendance_date=date(2026, 5, 22),
            )
            .first()
        )
        assert row.status == AttendanceStatus.ABSENT.value
        assert row.punch_in_time is None
        assert row.leave_record_id == approved_partial_morning_leave.id
        assert row.partial_leave_hours == Decimal("4.00")

    def test_u5_apply_hourly_with_existing_normal(
        self, db_session, sample_employee, approved_partial_hour_leave
    ):
        """U-5: apply 小時假 1.5hr + 既有 NORMAL row → NORMAL / partial_leave_hours=1.5"""
        existing = Attendance(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.NORMAL.value,
            punch_in_time=datetime.combine(date(2026, 5, 22), time(8, 50)),
            punch_out_time=datetime.combine(date(2026, 5, 22), time(18, 0)),
        )
        db_session.add(existing)
        db_session.commit()

        sync.apply(db_session, approved_partial_hour_leave.id)
        db_session.flush()

        row = (
            db_session.query(Attendance)
            .filter_by(
                employee_id=sample_employee.id,
                attendance_date=date(2026, 5, 22),
            )
            .first()
        )
        assert row.status == AttendanceStatus.NORMAL.value
        assert row.punch_in_time is not None
        assert row.punch_in_time.time() == time(8, 50)
        assert row.partial_leave_hours == Decimal("1.5")
        assert row.leave_record_id == approved_partial_hour_leave.id


class TestApplyExceptionPaths:
    def test_u6_apply_unapproved_leave_raises(self, db_session, sample_employee):
        """U-6: apply 對 unapproved leave → raise LeaveNotApproved"""
        from models.leave import LeaveRecord

        lv = LeaveRecord(
            employee_id=sample_employee.id,
            leave_type="personal",
            start_date=date(2026, 5, 22),
            end_date=date(2026, 5, 22),
            leave_hours=8.0,
            is_approved=None,  # pending
        )
        db_session.add(lv)
        db_session.commit()

        with pytest.raises(sync.LeaveNotApproved):
            sync.apply(db_session, lv.id)

    def test_u7_apply_idempotent_no_change_on_second_call(
        self, db_session, sample_employee, approved_full_day_leave
    ):
        """U-7: apply 重跑兩次 → 第二次 no-op，row 不變"""
        sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()
        rows_first = (
            db_session.query(Attendance).order_by(Attendance.attendance_date).all()
        )
        snapshot_first = [
            (r.attendance_date, r.status, r.leave_record_id) for r in rows_first
        ]

        # 第二次
        sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()
        rows_second = (
            db_session.query(Attendance).order_by(Attendance.attendance_date).all()
        )
        snapshot_second = [
            (r.attendance_date, r.status, r.leave_record_id) for r in rows_second
        ]

        assert snapshot_first == snapshot_second
        assert len(rows_second) == 3  # 沒重複插

    def test_u8_apply_conflict_with_other_leave_id(
        self, db_session, sample_employee, approved_full_day_leave
    ):
        """U-8: apply 同日已有其他 leave_record_id → raise LeaveAttendanceConflict"""
        # 預先建一筆 row 帶不同 leave_record_id=9999
        existing = Attendance(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 23),
            status=AttendanceStatus.LEAVE.value,
            leave_record_id=9999,
        )
        db_session.add(existing)
        db_session.commit()

        with pytest.raises(sync.LeaveAttendanceConflict):
            sync.apply(db_session, approved_full_day_leave.id)

    def test_u15_apply_partial_missing_time_raises(self, db_session, sample_employee):
        """U-15: apply 對部分請假但缺 start_time/end_time → raise LeavePartialTimeMissing"""
        from models.leave import LeaveRecord

        lv = LeaveRecord(
            employee_id=sample_employee.id,
            leave_type="personal",
            start_date=date(2026, 5, 22),
            end_date=date(2026, 5, 22),
            leave_hours=4.0,
            start_time=None,  # 故意缺
            end_time=None,
            is_approved=True,
        )
        db_session.add(lv)
        db_session.commit()

        with pytest.raises(sync.LeavePartialTimeMissing):
            sync.apply(db_session, lv.id)


# ── TestRevert ─────────────────────────────────────────────────────


class TestRevert:
    def test_u9_revert_full_day_no_punch_deletes_row(
        self, db_session, sample_employee, approved_full_day_leave
    ):
        """U-9: revert 全天假(無 punch) → attendance row 刪除"""
        # 先 apply 建出 3 筆 LEAVE row
        sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()

        rows_before = (
            db_session.query(Attendance)
            .filter_by(employee_id=sample_employee.id)
            .count()
        )
        assert rows_before == 3

        # revert
        reverted = sync.revert(db_session, approved_full_day_leave.id)
        db_session.flush()

        assert sorted(reverted) == [
            date(2026, 5, 22),
            date(2026, 5, 23),
            date(2026, 5, 24),
        ]

        rows_after = (
            db_session.query(Attendance)
            .filter_by(employee_id=sample_employee.id)
            .count()
        )
        assert rows_after == 0

    def test_u10_revert_full_day_with_punch_restores_normal(
        self, db_session, sample_employee, approved_full_day_leave
    ):
        """U-10: revert 全天假但有 punch(髒資料) → 退回 NORMAL / 清 leave_record_id"""
        # 先 apply
        sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()

        # 模擬髒資料:5/22 的 row 加上 punch
        row = (
            db_session.query(Attendance)
            .filter_by(
                employee_id=sample_employee.id,
                attendance_date=date(2026, 5, 22),
            )
            .first()
        )
        row.punch_in_time = datetime.combine(date(2026, 5, 22), time(9, 0))
        row.punch_out_time = datetime.combine(date(2026, 5, 22), time(18, 0))
        db_session.flush()

        sync.revert(db_session, approved_full_day_leave.id)
        db_session.flush()

        # 5/23 / 5/24 無 punch → 刪除; 5/22 有 punch → 保留
        remaining = (
            db_session.query(Attendance).filter_by(employee_id=sample_employee.id).all()
        )
        assert len(remaining) == 1
        row_after = remaining[0]
        assert row_after.attendance_date == date(2026, 5, 22)
        assert row_after.status == AttendanceStatus.NORMAL.value
        assert row_after.leave_record_id is None
        assert row_after.partial_leave_hours is None

    def test_u11_revert_partial_restores_late_minutes(
        self, db_session, sample_employee, approved_partial_morning_leave
    ):
        """U-11: revert 半天假 → punch 保留 / late 重算回 30 / 清 leave_record_id+partial"""
        # 先建一個 LATE row（punch 09:30）再 apply
        existing = Attendance(
            employee_id=sample_employee.id,
            attendance_date=date(2026, 5, 22),
            status=AttendanceStatus.LATE.value,
            punch_in_time=datetime.combine(date(2026, 5, 22), time(9, 30)),
            late_minutes=30,
        )
        db_session.add(existing)
        db_session.commit()

        sync.apply(db_session, approved_partial_morning_leave.id)
        db_session.flush()

        # apply 後 late_minutes 應歸零（leave 涵蓋 09:00-13:00）
        row = (
            db_session.query(Attendance)
            .filter_by(
                employee_id=sample_employee.id,
                attendance_date=date(2026, 5, 22),
            )
            .first()
        )
        assert row.late_minutes == 0

        # revert
        sync.revert(db_session, approved_partial_morning_leave.id)
        db_session.flush()

        row_after = (
            db_session.query(Attendance)
            .filter_by(
                employee_id=sample_employee.id,
                attendance_date=date(2026, 5, 22),
            )
            .first()
        )
        # punch 仍在
        assert row_after.punch_in_time is not None
        assert row_after.punch_in_time.time() == time(9, 30)
        # leave_record_id / partial_leave_hours 清掉
        assert row_after.leave_record_id is None
        assert row_after.partial_leave_hours is None
        # late_minutes 重算回 30（09:30 - 09:00 = 30，無 leave 加成）
        assert row_after.late_minutes == 30

    def test_u12_revert_idempotent(
        self, db_session, sample_employee, approved_full_day_leave
    ):
        """U-12: revert 重跑兩次 → no-op，第二次 reverted=[]"""
        sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()

        sync.revert(db_session, approved_full_day_leave.id)
        db_session.flush()

        # 第二次 revert 對同一 leave_id → 已無 row → 回傳空 list
        reverted_second = sync.revert(db_session, approved_full_day_leave.id)
        db_session.flush()

        assert reverted_second == []

        # 確認 DB 無殘留
        count = (
            db_session.query(Attendance)
            .filter_by(employee_id=sample_employee.id)
            .count()
        )
        assert count == 0


# ── TestReapply ─────────────────────────────────────────────────────


class TestReapply:
    def test_u13_reapply_changes_dates(
        self, db_session, sample_employee, approved_full_day_leave
    ):
        """U-13: reapply 改日期 5/22-5/24 → 5/23-5/25
        → 5/22 還原（刪除）; 5/25 新建; 5/23/5/24 保留
        """
        # 先 apply 原範圍(5/22-5/24)
        sync.apply(db_session, approved_full_day_leave.id)
        db_session.flush()

        # snapshot 舊範圍（caller 在 commit 前抓）
        old_snapshot = {
            "start_date": date(2026, 5, 22),
            "end_date": date(2026, 5, 24),
            "start_time": None,
            "end_time": None,
            "leave_type": "personal",
            "leave_hours": 8.0,
        }

        # 改 leave 範圍到 5/23-5/25
        approved_full_day_leave.start_date = date(2026, 5, 23)
        approved_full_day_leave.end_date = date(2026, 5, 25)
        db_session.commit()

        reverted, applied = sync.reapply(
            db_session,
            approved_full_day_leave.id,
            old_snapshot=old_snapshot,
        )
        db_session.flush()

        rows = db_session.query(Attendance).order_by(Attendance.attendance_date).all()
        dates = [r.attendance_date for r in rows]
        # 5/22 被刪，5/23/5/24 保留，5/25 新建
        assert dates == [date(2026, 5, 23), date(2026, 5, 24), date(2026, 5, 25)]
        for row in rows:
            assert row.status == AttendanceStatus.LEAVE.value
            assert row.leave_record_id == approved_full_day_leave.id

    def test_u14_reapply_full_day_to_partial(self, db_session, sample_employee):
        """U-14: reapply 改 leave_hours 8→4(全天變半天) + 補 start_time/end_time
        → 該日從 LEAVE 變 ABSENT + partial_leave_hours=4
        """
        from models.leave import LeaveRecord

        lv = LeaveRecord(
            employee_id=sample_employee.id,
            leave_type="personal",
            start_date=date(2026, 5, 22),
            end_date=date(2026, 5, 22),
            leave_hours=8.0,
            is_approved=True,
        )
        db_session.add(lv)
        db_session.commit()

        # 第一次 apply（全天）
        sync.apply(db_session, lv.id)
        db_session.flush()
        row = db_session.query(Attendance).first()
        assert row.status == AttendanceStatus.LEAVE.value

        # snapshot 舊狀態
        old_snapshot = {
            "start_date": date(2026, 5, 22),
            "end_date": date(2026, 5, 22),
            "start_time": None,
            "end_time": None,
            "leave_type": "personal",
            "leave_hours": 8.0,
        }

        # 改 leave_hours 為 4（半天）+ 補 start_time/end_time
        lv.leave_hours = 4.0
        lv.start_time = "09:00"
        lv.end_time = "13:00"
        db_session.commit()

        sync.reapply(db_session, lv.id, old_snapshot=old_snapshot)
        db_session.flush()

        row = db_session.query(Attendance).first()
        # revert 後舊 row（無 punch）被刪，apply 半天無 punch → 新建 ABSENT row
        assert row is not None
        assert row.status == AttendanceStatus.ABSENT.value
        assert row.partial_leave_hours == Decimal("4.00")
        assert row.leave_record_id == lv.id
