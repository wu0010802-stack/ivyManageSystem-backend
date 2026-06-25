"""F-B 回歸：多日部分請假時，partial_leave_hours 必須 per-day 攤分，否則扣薪乘以天數。

現況：_apply_partial（sync）與 attendance_leave_merge 對多日部分假每天各寫整筆
leave_hours → 薪資 _sum_leave_deduction 逐列取 partial → N 天 × leave_hours，
與曠職側 engine._compute_absence 的 per_day = lv_hours/span_days 口徑相反。

reachability：LeaveCreate/LeaveUpdate validator 只要求 leave_hours<8 時補
start_time/end_time，不要求單日（validate_leave_date_order 只擋跨月/順序）。
故多日 + 部分時數可達；admin 直接餵 LeaveRecord 也可達。

修法：寫 partial_leave_hours 時改 per-day 攤分 = leave_hours / span_days，
單日 span=1 不受影響。3 天、leave_hours=4 → 每天 4/3≈1.333；薪資總扣
≈ 單筆 4h 金額（≈500，daily=1000）而非 1500。
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
        base_salary=30000,
        is_active=True,
        work_start_time="09:00",
        work_end_time="18:00",
    )
    db_session.add(emp)
    db_session.commit()
    return emp


@pytest.fixture
def multiday_partial_leave(db_session, sample_employee):
    """7/6~7/8（3 天）部分 personal 假，leave_hours=4（整段 4h，非每天 4h）。"""
    from models.leave import LeaveRecord

    lv = LeaveRecord(
        employee_id=sample_employee.id,
        leave_type="personal",
        start_date=date(2026, 7, 6),
        end_date=date(2026, 7, 8),
        leave_hours=4.0,
        start_time="09:00",
        end_time="13:00",
        status="approved",
    )
    db_session.add(lv)
    db_session.commit()
    return lv


def _total_leave_deduction(rows, lv):
    """以 Attendance 為 SoT 算多列請假扣款（引擎口徑）。daily=1000。"""
    from services.salary.utils import _sum_leave_deduction

    pairs = [(r, lv) for r in rows]
    return _sum_leave_deduction(pairs, daily_salary=1000.0)


class TestMultiDayPartialPerDay:
    def test_sync_apply_per_day_attribution(
        self, db_session, sample_employee, multiday_partial_leave
    ):
        """sync.apply 多日部分假 → 每列 partial = 4/3，總扣 ≈ 單筆 4h 金額。"""
        sync.apply(db_session, multiday_partial_leave.id)
        db_session.flush()

        rows = (
            db_session.query(Attendance)
            .filter_by(employee_id=sample_employee.id)
            .order_by(Attendance.attendance_date)
            .all()
        )
        assert len(rows) == 3
        # 每列 partial = 4/3 ≈ 1.33，不可是 4
        for r in rows:
            assert float(r.partial_leave_hours) == pytest.approx(4.0 / 3.0, abs=0.01)

        # 總扣款 = 4h（攤分後合計）→ (4/8)*1000*1.0 = 500，而非 1500
        total = _total_leave_deduction(rows, multiday_partial_leave)
        assert total == pytest.approx(500.0, abs=2.0)

    def test_merge_per_day_attribution(
        self, db_session, sample_employee, multiday_partial_leave
    ):
        """merge_attendance_with_leave 多日部分假 → 每列 partial = 4/3。"""
        rows = []
        for d in (date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)):
            r = Attendance(
                employee_id=sample_employee.id,
                attendance_date=d,
                status=AttendanceStatus.NORMAL.value,
                punch_in_time=datetime.combine(d, time(13, 0)),
                punch_out_time=datetime.combine(d, time(18, 0)),
            )
            merge_attendance_with_leave(r, db_session)
            rows.append(r)

        for r in rows:
            assert float(r.partial_leave_hours) == pytest.approx(4.0 / 3.0, abs=0.01)

        total = _total_leave_deduction(rows, multiday_partial_leave)
        assert total == pytest.approx(500.0, abs=2.0)

    def test_single_day_partial_unaffected(self, db_session, sample_employee):
        """單日部分假 span=1 不受影響：partial = leave_hours。"""
        from models.leave import LeaveRecord

        lv = LeaveRecord(
            employee_id=sample_employee.id,
            leave_type="personal",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 6),
            leave_hours=4.0,
            start_time="09:00",
            end_time="13:00",
            status="approved",
        )
        db_session.add(lv)
        db_session.commit()

        sync.apply(db_session, lv.id)
        db_session.flush()

        row = (
            db_session.query(Attendance)
            .filter_by(employee_id=sample_employee.id, attendance_date=date(2026, 7, 6))
            .first()
        )
        assert row.partial_leave_hours == Decimal("4.00")
