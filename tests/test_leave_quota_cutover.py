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
        """跨學年 114-2 → 115-1：每位 active 員工生 6 種假別 new row。"""
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
        # QUOTA_LEAVE_TYPES = {annual, sick, menstrual, personal, family_care} + compensatory
        types = {r.leave_type for r in rows}
        assert types == {
            "annual",
            "sick",
            "menstrual",
            "personal",
            "family_care",
            "compensatory",
        }

    def test_annual_uses_new_term_start_date_as_reference(self, db_session):
        """特休 reference = new.start_date：2020/9/1 入職、2026/8/1 翻牌 → 年資 ~6年 → 120小時。"""
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        emp = _make_emp(db_session, hire_date=date(2020, 9, 1))

        handle(old=old, new=new, session=db_session)

        annual = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "annual",
            )
            .first()
        )
        # 2020/9/1 → 2026/8/1 = 5 年 11 個月 = 5 完整年 → 14 天 = 112 小時
        # (complete_years=5 進入 elif complete_years < 10 區段 → 120)
        # 仔細算：months=71，complete_years=5，<10 區間 → 120 小時
        assert annual.total_hours == 120.0

    def test_hire_date_none_yields_zero_annual(self, db_session):
        """hire_date 為 None：annual quota 0、note 提示。"""
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        emp = _make_emp(db_session, hire_date=None)

        handle(old=old, new=new, session=db_session)

        annual = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "annual",
            )
            .first()
        )
        assert annual.total_hours == 0.0

    def test_inactive_employee_no_quota_created(self, db_session):
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        _make_emp(db_session, name="離職員工", is_active=False)
        _make_emp(db_session, name="在職員工", is_active=True)

        handle(old=old, new=new, session=db_session)

        assert (
            db_session.query(LeaveQuota).filter(LeaveQuota.school_year == 115).count()
            == 6  # 只有在職員工的 6 筆
        )

    def test_compensatory_balance_carry_over(self, db_session):
        """補休結餘 carry-over：舊 row 16h、已核准用 6h → 新 row 10h。"""
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        emp = _make_emp(db_session)

        # 舊學年補休 quota row（school_year=114）
        old_comp = LeaveQuota(
            employee_id=emp.id,
            year=2026,
            school_year=114,
            leave_type="compensatory",
            total_hours=16.0,
        )
        db_session.add(old_comp)
        # 已核准請假 6 小時，在 old term 區間內
        used = LeaveRecord(
            employee_id=emp.id,
            leave_type="compensatory",
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 1),
            leave_hours=6.0,
            is_approved=True,
        )
        db_session.add(used)
        db_session.flush()

        handle(old=old, new=new, session=db_session)

        new_comp = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "compensatory",
            )
            .first()
        )
        assert new_comp.total_hours == pytest.approx(10.0)

    def test_compensatory_cold_start_falls_back_to_legacy_year_row(self, db_session):
        """First toggle 時系統內只有 legacy year-only row → fallback 查到、結餘正確 carry-over。

        Cold-start 真實情境：cutover 前 system 從未 emit term.changed，
        所有 leave_quotas row 都是 school_year=NULL + year=西元年。
        若 _calc_compensatory_balance 只查 school_year=old.school_year，會找不到 row、
        全員補休 silently 變 0 = P0 data-loss bug。
        """
        old = _make_term(db_session, 114, 2, date(2026, 2, 1), date(2026, 7, 31))
        new = _make_term(db_session, 115, 1, date(2026, 8, 1), date(2027, 1, 31))
        emp = _make_emp(db_session)

        # 模擬 first cutover 前的狀態：只有 legacy year-only row（school_year=NULL）
        legacy = LeaveQuota(
            employee_id=emp.id,
            year=2026,
            school_year=None,
            leave_type="compensatory",
            total_hours=20.0,
        )
        db_session.add(legacy)
        db_session.flush()

        handle(old=old, new=new, session=db_session)

        new_comp = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "compensatory",
            )
            .first()
        )
        # 結餘應從 legacy row 拿到（20h），沒被誤判為 0
        assert new_comp.total_hours == pytest.approx(20.0)
        assert "carry-over" in (new_comp.note or "")

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
