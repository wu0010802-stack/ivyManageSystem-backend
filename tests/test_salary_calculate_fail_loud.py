"""Task 6：缺年度設定的 fail-loud 整批中止機制。

config_for_month 在 bulk（process_bulk_salary_calculation）與 simulate
（_compute_period_accrual_totals）的「員工迴圈 / 任何寫入之前」進入，故缺該年度設定時
會在此 raise → 整批中止、零寫入。API 層再把 PayrollConfigMissingError 轉 422。
此處驗證引擎層機制；API 422 對應見 calculate.py / simulate.py 的 except 子句。
"""

import pytest

from models.database import InsuranceRate
from services.salary.engine import SalaryEngine
from services.salary.config_resolver import PayrollConfigMissingError


def test_config_for_month_raises_when_populated_but_year_missing(test_db_session):
    """設定表有資料但缺該年度 → config_for_month 進入時 fail-loud。

    config_for_month 在 bulk/simulate 的寫入與員工迴圈之前進入，故此 raise 等同整批中止、零寫入。
    """
    s = test_db_session
    s.add(InsuranceRate(rate_year=2026, version=1))  # 表非空、缺 2099
    s.flush()
    engine = SalaryEngine(load_from_db=False)
    with pytest.raises(PayrollConfigMissingError):
        with engine.config_for_month(s, 2099, 1):
            pass


def test_config_for_month_empty_tables_no_raise(test_db_session):
    """所有設定表皆空（dev/全新部署）→ 不 fail-loud，沿用引擎預設。"""
    s = test_db_session
    engine = SalaryEngine(load_from_db=False)
    with engine.config_for_month(s, 2099, 1):
        pass  # 不應 raise
