"""leave_quota_cutover subscriber 單元測試。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, LeaveQuota, LeaveRecord
from models.academic_term import AcademicTerm
from models.overtime import OvertimeRecord
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant
from models.unused_leave_payout_log import (
    UnusedLeavePayoutLog,
)  # noqa: F401 — 確保 FK target 表進 metadata
from services.term_subscribers.leave_quota_cutover import handle
from utils.term_events import reset_handlers_for_tests


@pytest.fixture
def db_session(tmp_path):
    """SQLite in-memory test session（swap base_module 全域 engine pattern）。"""
    db_path = tmp_path / "term.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    session = session_factory()
    yield session
    session.close()

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture(autouse=True)
def _reset():
    reset_handlers_for_tests()
    yield
    reset_handlers_for_tests()


def _make_term(db_session, sy, sem, sd, ed, is_current=False):
    t = AcademicTerm(
        school_year=sy,
        semester=sem,
        start_date=sd,
        end_date=ed,
        is_current=is_current,
    )
    db_session.add(t)
    db_session.flush()
    return t


_emp_counter = 0


def _make_emp(db_session, name="員工A", hire_date=date(2020, 9, 1), is_active=True):
    global _emp_counter
    _emp_counter += 1
    e = Employee(
        employee_id=f"TEST{_emp_counter:04d}",
        name=name,
        hire_date=hire_date,
        is_active=is_active,
    )
    db_session.add(e)
    db_session.flush()
    return e


class TestLeaveQuotaCutover:
    def test_initial_set_current_no_op(self, db_session):
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31), True)
        handle(old=None, new=new, session=db_session)
        assert db_session.query(LeaveQuota).count() == 0

    def test_same_year_no_op(self, db_session):
        """同學年 1→2 不 cutover quota。"""
        old = _make_term(db_session, 114, 1, date(2025, 8, 1), date(2026, 1, 31))
        new = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        _make_emp(db_session)
        handle(old=old, new=new, session=db_session)
        assert db_session.query(LeaveQuota).count() == 0

    def test_cross_year_creates_new_row_for_each_active_employee(self, db_session):
        """跨學年 114-2 → 115-1：每位 active 員工生 5 種假別 new row（annual 由 anniversary scheduler 建）。"""
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        emp = _make_emp(db_session, hire_date=date(2020, 9, 1))

        handle(old=old, new=new, session=db_session)

        rows = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
            )
            .all()
        )
        # QUOTA_LEAVE_TYPES 中 annual 由 anniversary scheduler 負責，不在此 cutover 建立
        # cutover 建：sick, menstrual, personal, family_care + compensatory carry-over
        types = {r.leave_type for r in rows}
        assert types == {
            "sick",
            "menstrual",
            "personal",
            "family_care",
            "compensatory",
        }

    def test_inactive_employee_no_quota_created(self, db_session):
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        _make_emp(db_session, name="離職員工", is_active=False)
        _make_emp(db_session, name="在職員工", is_active=True)

        handle(old=old, new=new, session=db_session)

        assert (
            db_session.query(LeaveQuota).filter(LeaveQuota.school_year == 115).count()
            == 5  # 只有在職員工的 5 筆（annual 由 anniversary scheduler 負責）
        )

    def test_cutover_does_not_create_annual_row_anymore(self, db_session):
        """跨學年 cutover 不再建 annual row（由 anniversary scheduler 負責）。"""
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        _make_emp(db_session, hire_date=date(2020, 4, 1))

        handle(old=old, new=new, session=db_session)

        annual_rows = (
            db_session.query(LeaveQuota)
            .filter(LeaveQuota.leave_type == "annual", LeaveQuota.school_year == 115)
            .all()
        )
        assert len(annual_rows) == 0  # 不再建，由 anniversary scheduler 處理

    def test_cutover_compensatory_carryover_uses_grant_sum(self, db_session):
        """補休 carry-over 數值 = 當下 active grants SUM(granted_hours - consumed_hours)。"""
        emp = _make_emp(db_session)

        # 建 OT 與 grant（8h granted, 2h consumed → 6h balance）
        ot = OvertimeRecord(
            employee_id=emp.id,
            overtime_date=date(2025, 4, 1),
            overtime_type="weekday",
            hours=8.0,
            use_comp_leave=True,
            comp_leave_granted=True,
            is_approved=True,
        )
        db_session.add(ot)
        db_session.flush()
        grant = OvertimeCompLeaveGrant(
            overtime_record_id=ot.id,
            employee_id=emp.id,
            granted_hours=8.0,
            granted_at=date(2025, 4, 1),
            expires_at=date(2026, 4, 1),
            consumed_hours=2.0,
            status="active",
        )
        db_session.add(grant)
        db_session.flush()

        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))

        handle(old=old, new=new, session=db_session)

        comp = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "compensatory",
            )
            .first()
        )
        assert comp is not None
        assert comp.total_hours == pytest.approx(6.0)  # 8 - 2

    def test_idempotent_repeated_handle(self, db_session):
        """同 school_year row 已存在則 skip，不會 double-insert。"""
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        emp = _make_emp(db_session)

        handle(old=old, new=new, session=db_session)
        first_count = (
            db_session.query(LeaveQuota).filter(LeaveQuota.school_year == 115).count()
        )

        # 第二次跑（admin 不小心連按）
        handle(old=old, new=new, session=db_session)
        second_count = (
            db_session.query(LeaveQuota).filter(LeaveQuota.school_year == 115).count()
        )
        assert first_count == second_count
