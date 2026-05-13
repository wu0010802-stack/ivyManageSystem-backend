"""驗證 finalize_guard：跨月、單日、空集合、finalized 偵測。"""

import os
import sys
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, SalaryRecord
from services.salary.finalize_guard import (
    collect_months_from_range,
    collect_months_from_dates,
    assert_months_not_finalized,
)


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "finalize-guard.sqlite"
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
    try:
        yield session
    finally:
        session.close()
        base_module._engine = old_engine
        base_module._SessionFactory = old_session_factory
        engine.dispose()


def _finalize(session, *, employee_id, year, month):
    rec = SalaryRecord(
        employee_id=employee_id,
        salary_year=year,
        salary_month=month,
        is_finalized=True,
        finalized_by="test_admin",
    )
    session.add(rec)
    session.flush()
    return rec


def test_collect_months_from_range_single_month():
    assert collect_months_from_range(date(2026, 5, 1), date(2026, 5, 31)) == {(2026, 5)}


def test_collect_months_from_range_cross_two_months():
    assert collect_months_from_range(date(2026, 5, 28), date(2026, 6, 2)) == {
        (2026, 5),
        (2026, 6),
    }


def test_collect_months_from_range_cross_year():
    assert collect_months_from_range(date(2025, 12, 30), date(2026, 1, 5)) == {
        (2025, 12),
        (2026, 1),
    }


def test_collect_months_from_dates_single_day():
    assert collect_months_from_dates([date(2026, 5, 15)]) == {(2026, 5)}


def test_collect_months_from_dates_multiple():
    assert collect_months_from_dates([date(2026, 5, 15), date(2026, 6, 1)]) == {
        (2026, 5),
        (2026, 6),
    }


def test_assert_months_not_finalized_empty_set_is_noop(db_session):
    assert_months_not_finalized(db_session, employee_id=1, months=set())  # no raise


def test_assert_months_not_finalized_raises_when_any_finalized(db_session):
    _finalize(db_session, employee_id=1, year=2026, month=5)
    with pytest.raises(HTTPException) as exc:
        assert_months_not_finalized(
            db_session, employee_id=1, months={(2026, 5), (2026, 6)}
        )
    assert exc.value.status_code == 409
    assert "2026 年 5 月" in exc.value.detail


def test_assert_months_not_finalized_passes_when_none_finalized(db_session):
    assert_months_not_finalized(
        db_session, employee_id=1, months={(2026, 7), (2026, 8)}
    )  # no raise
