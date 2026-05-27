"""育嬰假/產假/流產假期間自動跳過獎金測試。

對齊《義華薪資》Excel 郭玟秀（114.11.03~114.12.28 產假 + 115.01.09~115.07.09 育嬰假）
與陳品棻（108.10.21~12.15 產假 + 109.01.01~110.12.31 育嬰假）案例。
"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, LeaveRecord
from services.leave_bonus_skip import (
    SKIP_BONUS_LEAVE_TYPES,
    format_skip_reason,
    should_skip_bonuses_for_month,
)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    sf = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)
    s = sf()
    yield s
    s.close()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _add_emp(session, name="郭玟秀"):
    emp = Employee(
        employee_id="E001",
        name=name,
        employee_type="regular",
        base_salary=30000,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _add_leave(session, emp_id, leave_type, start, end, approved=True):
    # Accept bool/None for backwards-compat
    if approved is True:
        _status = "approved"
    elif approved is False:
        _status = "rejected"
    else:
        _status = "pending"
    lv = LeaveRecord(
        employee_id=emp_id,
        leave_type=leave_type,
        start_date=start,
        end_date=end,
        status=_status,
    )
    session.add(lv)
    session.flush()
    return lv


class TestSkipDetection:
    def test_maternity_covering_month_skips(self, session):
        """產假覆蓋整個 4 月 → 該月應跳過。"""
        emp = _add_emp(session)
        _add_leave(session, emp.id, "maternity", date(2026, 4, 1), date(2026, 4, 30))
        session.commit()
        skip, leaves = should_skip_bonuses_for_month(session, emp.id, 2026, 4)
        assert skip is True
        assert len(leaves) == 1

    def test_parental_unpaid_covering_month_skips(self, session):
        """育嬰留職停薪覆蓋月份 → 跳過。"""
        emp = _add_emp(session)
        _add_leave(
            session,
            emp.id,
            "parental_unpaid",
            date(2026, 1, 9),
            date(2026, 7, 9),
        )
        session.commit()
        # 1 月（部分覆蓋）
        skip1, _ = should_skip_bonuses_for_month(session, emp.id, 2026, 1)
        assert skip1 is True
        # 4 月（完整覆蓋）
        skip4, _ = should_skip_bonuses_for_month(session, emp.id, 2026, 4)
        assert skip4 is True
        # 7 月（部分覆蓋至 7/9）
        skip7, _ = should_skip_bonuses_for_month(session, emp.id, 2026, 7)
        assert skip7 is True
        # 8 月（無覆蓋）
        skip8, _ = should_skip_bonuses_for_month(session, emp.id, 2026, 8)
        assert skip8 is False

    def test_miscarriage_skips(self, session):
        emp = _add_emp(session)
        _add_leave(session, emp.id, "miscarriage", date(2026, 4, 1), date(2026, 4, 30))
        session.commit()
        skip, _ = should_skip_bonuses_for_month(session, emp.id, 2026, 4)
        assert skip is True

    def test_personal_leave_does_not_skip(self, session):
        """事假/病假/生理假等不在 skip 名單。"""
        emp = _add_emp(session)
        _add_leave(session, emp.id, "personal", date(2026, 4, 1), date(2026, 4, 30))
        _add_leave(session, emp.id, "sick", date(2026, 4, 15), date(2026, 4, 16))
        session.commit()
        skip, _ = should_skip_bonuses_for_month(session, emp.id, 2026, 4)
        assert skip is False

    def test_marriage_does_not_skip_by_default(self, session):
        """婚假預設不在 skip 名單（業主可後續加）。"""
        emp = _add_emp(session)
        _add_leave(session, emp.id, "marriage", date(2026, 4, 1), date(2026, 4, 8))
        session.commit()
        skip, _ = should_skip_bonuses_for_month(session, emp.id, 2026, 4)
        assert skip is False

    def test_unapproved_leave_does_not_skip(self, session):
        """未核准假單不算（避免員工自助繞過）。"""
        emp = _add_emp(session)
        _add_leave(
            session,
            emp.id,
            "maternity",
            date(2026, 4, 1),
            date(2026, 4, 30),
            approved=False,
        )
        # status="rejected" 不會被 add_leave 改，但 query 過濾 True；測試 None / False 都應被排除
        session.query(LeaveRecord).update({LeaveRecord.status: "pending"})
        session.commit()
        skip, _ = should_skip_bonuses_for_month(session, emp.id, 2026, 4)
        assert skip is False

    def test_leave_outside_month_does_not_skip(self, session):
        emp = _add_emp(session)
        _add_leave(session, emp.id, "maternity", date(2026, 1, 1), date(2026, 3, 31))
        session.commit()
        skip, _ = should_skip_bonuses_for_month(session, emp.id, 2026, 4)
        assert skip is False

    def test_one_day_overlap_still_skips(self, session):
        """假期最後一天 = 該月第一天 → 仍 skip（業主慣例）。"""
        emp = _add_emp(session)
        _add_leave(session, emp.id, "maternity", date(2026, 3, 1), date(2026, 4, 1))
        session.commit()
        skip, leaves = should_skip_bonuses_for_month(session, emp.id, 2026, 4)
        assert skip is True
        assert len(leaves) == 1

    def test_custom_leave_types_override(self, session):
        """leave_types 參數可自訂（業主想加婚假時用）。"""
        emp = _add_emp(session)
        _add_leave(session, emp.id, "marriage", date(2026, 4, 1), date(2026, 4, 8))
        session.commit()
        skip, _ = should_skip_bonuses_for_month(
            session, emp.id, 2026, 4, leave_types=frozenset(["marriage"])
        )
        assert skip is True

    def test_format_skip_reason(self, session):
        emp = _add_emp(session)
        _add_leave(session, emp.id, "maternity", date(2026, 3, 1), date(2026, 4, 30))
        _add_leave(
            session,
            emp.id,
            "parental_unpaid",
            date(2026, 5, 1),
            date(2026, 10, 31),
        )
        session.commit()
        _, leaves = should_skip_bonuses_for_month(session, emp.id, 2026, 4)
        msg = format_skip_reason(leaves)
        assert "產假" in msg
        assert "2026-03-01" in msg


class TestEngineIntegration:
    """驗證 _compute_period_accrual_totals 跳過 skip 月份。"""

    def test_period_accrual_skips_maternity_month(self, session, monkeypatch):
        """3 個月期間累積中，1 個月育嬰 → 該月貢獻 0，期間總計 = 兩月合計。"""
        from services.salary.engine import SalaryEngine

        emp = _add_emp(session, name="郭玟秀")
        # 4 月育嬰假 → 該月不算
        _add_leave(
            session,
            emp.id,
            "parental_unpaid",
            date(2026, 4, 1),
            date(2026, 7, 9),
        )
        session.commit()

        engine = SalaryEngine(load_from_db=False)

        # Mock period_months = [2,3,4,5,6]（6 月發放）
        # mock calculate_period_accrual_row 回固定值（避免複雜 fixture）
        monkeypatch.setattr(
            "services.salary.utils.get_distribution_period_months",
            lambda y, m: [(2026, 2), (2026, 3), (2026, 4), (2026, 5)],
        )
        monkeypatch.setattr(
            engine,
            "calculate_period_accrual_row",
            lambda emp_id, y, m, _ctx=None: {
                "festival_bonus": 1000,
                "overtime_bonus": 200,
            },
        )

        # mock _resolve_classroom_for_employee_in_term 避免依賴
        monkeypatch.setattr(
            engine,
            "_resolve_classroom_for_employee_in_term",
            lambda *a, **kw: None,
        )

        festival, overtime = engine._compute_period_accrual_totals(
            session, emp, 2026, 6
        )
        # 4 個月 期間：2、3、4、5 月。4、5 月育嬰 → 各貢獻 0
        # 預期：(2、3 月各 1000) = 2000
        assert festival == 2000
        # overtime 同理：200 × 2 = 400
        assert overtime == 400
