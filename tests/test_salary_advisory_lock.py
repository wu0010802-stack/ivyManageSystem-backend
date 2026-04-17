"""tests/test_salary_advisory_lock.py — advisory_lock 工具單元測試。

在 SQLite 環境下 lock 應為 no-op（不拋錯、不阻塞）；
並驗證 key 計算：不同 (emp, year, month) 產生不同 key，同組合產生相同 key。
整月鎖與員工鎖 namespace 不同，不會碰撞。
"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base  # noqa: F401 metadata
from utils.advisory_lock import (
    _key_for_salary,
    acquire_salary_lock,
    salary_lock,
    try_salary_lock,
)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


class TestKeyCalculation:
    def test_same_input_same_key(self):
        assert _key_for_salary(42, 2026, 4) == _key_for_salary(42, 2026, 4)

    def test_different_employee_different_key(self):
        assert _key_for_salary(1, 2026, 4) != _key_for_salary(2, 2026, 4)

    def test_different_month_different_key(self):
        assert _key_for_salary(1, 2026, 3) != _key_for_salary(1, 2026, 4)

    def test_month_lock_vs_employee_lock_different_namespace(self):
        """整月鎖（employee_id=None）與員工鎖不會碰撞。"""
        assert _key_for_salary(None, 2026, 4) != _key_for_salary(1, 2026, 4)

    def test_key_fits_positive_int63(self):
        k = _key_for_salary(999, 2099, 12)
        assert 0 <= k <= 0x7FFF_FFFF_FFFF_FFFF


class TestSqliteFallback:
    def test_acquire_no_op_on_sqlite(self, session):
        # SQLite 下應直接返回，不拋錯
        acquire_salary_lock(session, employee_id=1, year=2026, month=4)
        acquire_salary_lock(session, year=2026, month=4)  # 整月鎖

    def test_context_manager_yields_on_sqlite(self, session):
        with salary_lock(session, employee_id=1, year=2026, month=4):
            pass  # 不應阻塞或拋錯

    def test_try_lock_returns_true_on_sqlite(self, session):
        with try_salary_lock(session, employee_id=1, year=2026, month=4) as ok:
            assert ok is True
