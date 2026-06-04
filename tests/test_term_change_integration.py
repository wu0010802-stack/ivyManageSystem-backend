"""term.changed subscriber 整合測試（改由 fire_term_changed 直接驅動）。

set-current 端點已移除，學期切換改由 academic_term_turnover_scheduler 驅動。
本檔聚焦 subscriber（classroom_carry_over / leave_quota_cutover）在一次
term.changed 中的行為；turnover 觸發/seed/idempotent 的 reconcile 層覆蓋見
tests/test_academic_term_turnover_scheduler.py。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import (  # noqa: E402
    Base,
    Classroom,
    Employee,
    LeaveQuota,
    Student,
)
from models.academic_term import AcademicTerm  # noqa: E402
from models.overtime import OvertimeRecord  # noqa: E402,F401
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant  # noqa: E402,F401
from models.unused_leave_payout_log import UnusedLeavePayoutLog  # noqa: E402,F401
from utils.term_events import (  # noqa: E402
    fire_term_changed,
    register_handler,
    reset_handlers_for_tests,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()

    from services.term_subscribers.classroom_carry_over import handle as cco
    from services.term_subscribers.leave_quota_cutover import handle as lqc
    from services.term_subscribers.activity_semester_tag import handle as ast

    reset_handlers_for_tests()
    register_handler("classroom_carry_over", cco)
    register_handler("leave_quota_cutover", lqc)
    register_handler("activity_semester_tag_reset", ast)

    yield s

    reset_handlers_for_tests()
    s.close()


def _seed_term(session, *, school_year, semester, is_current=False):
    from utils.academic import term_bounds

    start, end = term_bounds(school_year, semester)
    t = AcademicTerm(
        school_year=school_year,
        semester=semester,
        start_date=start,
        end_date=end,
        is_current=is_current,
    )
    session.add(t)
    session.flush()
    return t


def _seed_classroom(session, sy, sem, name="ABC"):
    cls = Classroom(name=name, school_year=sy, semester=sem, capacity=30)
    session.add(cls)
    session.flush()
    return cls


def _seed_student(session, classroom_id, student_id):
    s = Student(
        student_id=student_id,
        name=f"S{student_id}",
        gender="M",
        birthday=date(2020, 1, 1),
        classroom_id=classroom_id,
        is_active=True,
    )
    session.add(s)
    session.flush()
    return s


_emp_counter = 0


def _seed_emp(session, hire_date=date(2020, 9, 1)):
    global _emp_counter
    _emp_counter += 1
    e = Employee(
        employee_id=f"E{_emp_counter:03d}",
        name="員工",
        hire_date=hire_date,
        is_active=True,
    )
    session.add(e)
    session.flush()
    return e


def _fire(session, old, new):
    """模擬 reconcile 翻牌後呼叫 subscriber：先 toggle is_current 再 fire。"""
    if old is not None:
        old.is_current = False
        session.flush()
    new.is_current = True
    session.flush()
    fire_term_changed(old=old, new=new, session=session)
    session.flush()


class TestTermChangeSubscribers:
    def test_same_year_1_to_2_classroom_carry_over(self, db_session):
        """114-1 → 114-2：classroom 複製、學生遷移、quota 不動。"""
        old_t = _seed_term(db_session, school_year=114, semester=1, is_current=True)
        new_t = _seed_term(db_session, school_year=114, semester=2)
        old_cls = _seed_classroom(db_session, 114, 1, name="星星班")
        s = _seed_student(db_session, old_cls.id, "114-A-01")

        _fire(db_session, old_t, new_t)

        new_cls = (
            db_session.query(Classroom)
            .filter(Classroom.school_year == 114, Classroom.semester == 2)
            .first()
        )
        assert new_cls is not None
        assert new_cls.name == "星星班"
        db_session.refresh(s)
        assert s.classroom_id == new_cls.id
        assert db_session.query(LeaveQuota).count() == 0

    def test_cross_year_2_to_1_leave_quota_cutover(self, db_session):
        """114-2 → 115-1：classroom 不動、每員工生 5 筆 quota。"""
        old_t = _seed_term(db_session, school_year=114, semester=2, is_current=True)
        new_t = _seed_term(db_session, school_year=115, semester=1)
        emp = _seed_emp(db_session)

        _fire(db_session, old_t, new_t)

        rows = (
            db_session.query(LeaveQuota)
            .filter(LeaveQuota.employee_id == emp.id, LeaveQuota.school_year == 115)
            .all()
        )
        assert len(rows) == 5  # 4 QUOTA_LEAVE_TYPES + compensatory

    def test_cross_year_quota_compensatory_balance_carry_over(self, db_session):
        """補休結餘 carry-over：granted 8h、consumed 2h → 新 row 6h。"""
        old_t = _seed_term(db_session, school_year=114, semester=2, is_current=True)
        new_t = _seed_term(db_session, school_year=115, semester=1)
        emp = _seed_emp(db_session)
        ot = OvertimeRecord(
            employee_id=emp.id,
            overtime_date=date(2026, 3, 1),
            overtime_type="weekday",
            hours=8.0,
            use_comp_leave=True,
            comp_leave_granted=True,
            status="approved",
        )
        db_session.add(ot)
        db_session.flush()
        db_session.add(
            OvertimeCompLeaveGrant(
                overtime_record_id=ot.id,
                employee_id=emp.id,
                granted_hours=8.0,
                granted_at=date(2026, 3, 1),
                expires_at=date(2027, 3, 1),
                consumed_hours=2.0,
                status="active",
            )
        )
        db_session.flush()

        _fire(db_session, old_t, new_t)

        new_comp = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "compensatory",
            )
            .first()
        )
        assert new_comp.total_hours == pytest.approx(6.0)

    def test_cross_year_does_not_create_annual_quota(self, db_session):
        """特休週年制後 cutover 不建 annual row。"""
        old_t = _seed_term(db_session, school_year=114, semester=2, is_current=True)
        new_t = _seed_term(db_session, school_year=115, semester=1)
        emp = _seed_emp(db_session, hire_date=date(2020, 9, 1))

        _fire(db_session, old_t, new_t)

        annual = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "annual",
            )
            .first()
        )
        assert annual is None

    def test_idempotent_handler_does_not_double_insert_quotas(self, db_session):
        """raw 連呼叫兩次 leave_quota_cutover → quota 只一份。"""
        from services.term_subscribers.leave_quota_cutover import handle as lqc_handle

        old_t = _seed_term(db_session, school_year=114, semester=2)
        new_t = _seed_term(db_session, school_year=115, semester=1)
        _seed_emp(db_session)

        lqc_handle(old=old_t, new=new_t, session=db_session)
        db_session.flush()
        first = (
            db_session.query(LeaveQuota).filter(LeaveQuota.school_year == 115).count()
        )
        lqc_handle(old=old_t, new=new_t, session=db_session)
        db_session.flush()
        second = (
            db_session.query(LeaveQuota).filter(LeaveQuota.school_year == 115).count()
        )
        assert first == second == 5

    def test_atypical_jump_113_2_to_115_1_no_op(self, db_session, caplog):
        """跳級 113-2 → 115-1：classroom no-op + warning（直接呼叫驗 handler 分支）。"""
        import logging

        old_t = _seed_term(db_session, school_year=113, semester=2, is_current=True)
        new_t = _seed_term(db_session, school_year=115, semester=1)
        _seed_classroom(db_session, 113, 2)

        with caplog.at_level(logging.WARNING):
            _fire(db_session, old_t, new_t)

        assert (
            db_session.query(Classroom).filter(Classroom.school_year == 115).count()
            == 0
        )
        assert db_session.query(LeaveQuota).count() == 0
        assert any("非典型切換" in r.message for r in caplog.records)

    def test_read_path_prefers_school_year_falls_back_to_year(self, db_session):
        """_resolve_quota_row：school_year row 優先、缺則 fallback 西元年。

        resolve_current 改日期推導後，用 target_date=2026/9/1（→115學年）讓本測試
        與「今天」無關地驗證讀路徑優先序。
        """
        from api.leaves_quota import _resolve_quota_row

        emp = _seed_emp(db_session)
        new_row = LeaveQuota(
            employee_id=emp.id,
            year=2026,
            school_year=115,
            leave_type="annual",
            total_hours=120.0,
        )
        legacy_row = LeaveQuota(
            employee_id=emp.id,
            year=2026,
            school_year=None,
            leave_type="annual",
            total_hours=100.0,
        )
        db_session.add_all([new_row, legacy_row])
        db_session.flush()

        found = _resolve_quota_row(
            db_session, emp.id, "annual", target_date=date(2026, 9, 1)
        )
        assert found.id == new_row.id

        db_session.delete(new_row)
        db_session.flush()
        fallback = _resolve_quota_row(
            db_session, emp.id, "annual", target_date=date(2026, 9, 1)
        )
        assert fallback.id == legacy_row.id
