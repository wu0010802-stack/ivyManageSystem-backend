"""Pure function helpers for leave quota expiry — date/hourly wage resolvers."""

from datetime import date
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Column, Integer, Date, create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

from services.leave_quota_expiry.helpers import (
    _next_month,
    _add_one_year_with_feb29_handling,
    _resolve_hourly_wage,
    _is_anniversary_today_sql,
    _approved_annual_used_in_period,
    _find_or_none_salary_record,
    _compensatory_balance,
)


class TestNextMonth:
    """跨年 12→1 wrap"""

    def test_next_month_normal(self):
        assert _next_month(date(2026, 4, 15)) == (2026, 5)

    def test_next_month_year_wrap(self):
        assert _next_month(date(2026, 12, 31)) == (2027, 1)


class TestAddOneYearWithFeb29Handling:
    """2/29 + 1y 落非閏年順延 2/28"""

    def test_add_one_year_normal(self):
        assert _add_one_year_with_feb29_handling(date(2025, 4, 1)) == date(2026, 4, 1)

    def test_add_one_year_feb29_to_non_leap(self):
        # 2024 是閏年，2025 不是 → 2/29 → 2/28
        assert _add_one_year_with_feb29_handling(date(2024, 2, 29)) == date(2025, 2, 28)


class TestResolveHourlyWage:
    """月薪/30/8 或 hourly_rate"""

    def test_resolve_hourly_wage_hourly_employee(self):
        emp = MagicMock(employee_type="hourly", hourly_rate=200.0)
        assert _resolve_hourly_wage(emp, date(2026, 4, 1)) == 200.0

    def test_resolve_hourly_wage_monthly_employee(self):
        emp = MagicMock(employee_type="monthly", base_salary=48000.0)
        # 48000 / 30 / 8 = 200
        assert _resolve_hourly_wage(emp, date(2026, 4, 1)) == 200.0


# ──────────────────────────────────────────────────────────────────────────────
# SQL helpers tests — require session/DB
# ──────────────────────────────────────────────────────────────────────────────

Base = declarative_base()


class _DummyEmp(Base):
    __tablename__ = "dummy_emp"
    id = Column(Integer, primary_key=True)
    hire_date = Column(Date)


@pytest.fixture
def session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()


class TestIsAnniversaryTodaySQL:
    """Anniversary month/day matching with 2/29 fallback"""

    def test_is_anniversary_today_sql_match(self, session):
        session.add(_DummyEmp(id=1, hire_date=date(2020, 4, 15)))
        session.add(_DummyEmp(id=2, hire_date=date(2021, 5, 1)))
        session.commit()
        result = (
            session.query(_DummyEmp)
            .filter(_is_anniversary_today_sql(_DummyEmp.hire_date, date(2026, 4, 15)))
            .all()
        )
        assert len(result) == 1
        assert result[0].id == 1

    def test_is_anniversary_today_sql_feb29_in_non_leap(self, session):
        """2/29 員工在非閏年 2/28 也算 anniversary"""
        session.add(_DummyEmp(id=1, hire_date=date(2020, 2, 29)))
        session.commit()
        # 2026 非閏年，2/28 應命中
        result = (
            session.query(_DummyEmp)
            .filter(_is_anniversary_today_sql(_DummyEmp.hire_date, date(2026, 2, 28)))
            .all()
        )
        assert len(result) == 1
        assert result[0].id == 1


class TestCompensatoryBalance:
    """補休結餘 = SUM(granted_hours - consumed_hours) WHERE status='active'"""

    def test_compensatory_balance_sum_active_grants(self, session):
        """補休結餘 = SUM(granted_hours - consumed_hours) WHERE status='active'"""
        # 建立補休 grant 表（所有 FK 參考表均虛擬，實際不插入資料）
        session.execute(text("""CREATE TABLE overtime_comp_leave_grants (
            id INTEGER PRIMARY KEY,
            overtime_record_id INTEGER NOT NULL,
            employee_id INTEGER NOT NULL,
            granted_hours FLOAT NOT NULL,
            granted_at DATE NOT NULL,
            expires_at DATE NOT NULL,
            consumed_hours FLOAT NOT NULL DEFAULT 0,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            expired_at DATETIME,
            payout_salary_record_id INTEGER,
            payout_log_id BIGINT,
            created_at DATETIME,
            updated_at DATETIME
        )"""))
        session.commit()

        # 直接 SQL INSERT 測試資料（迴避 ORM FK 檢查）
        session.execute(text("""INSERT INTO overtime_comp_leave_grants
            (id, overtime_record_id, employee_id, granted_hours, granted_at,
             expires_at, consumed_hours, status)
            VALUES
            (1, 10, 1, 4.0, '2025-04-01', '2026-04-01', 1.0, 'active'),
            (2, 11, 1, 8.0, '2025-05-01', '2026-05-01', 0.0, 'active'),
            (3, 12, 1, 2.0, '2024-01-01', '2025-01-01', 0.0, 'expired')"""))
        session.commit()

        assert _compensatory_balance(1, session) == 11.0  # (4-1) + (8-0)
