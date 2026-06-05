"""Task 3 回歸測試：_select_active_at 改走 period-aware resolver 後行為驗證。"""

import pytest

from models.database import BonusConfig, InsuranceRate
from services.salary.engine import SalaryEngine
from services.salary.config_resolver import PayrollConfigMissingError


def _seed_bonus(s, year, head_ab):
    s.add(BonusConfig(config_year=year, version=1, head_teacher_ab=head_ab))


def test_select_active_at_resolves_by_year_not_latest(test_db_session):
    """同時有 2026/2027 BonusConfig，解析 2026 必須回 2026（舊版用 created_at 會誤回 2027）。"""
    s = test_db_session
    _seed_bonus(s, 2026, 2000)
    _seed_bonus(s, 2027, 9999)
    s.flush()
    row = SalaryEngine._select_active_at(s, BonusConfig, 2026, 1)
    assert row.config_year == 2026
    assert row.head_teacher_ab == 2000


def test_select_active_at_missing_year_raises(test_db_session):
    """表有設定列但缺該年度 → fail-loud（行政漏建年度的真實誤設）。"""
    s = test_db_session
    s.add(InsuranceRate(rate_year=2026, version=1))  # 表非空，但無 2099
    s.flush()
    with pytest.raises(PayrollConfigMissingError):
        SalaryEngine._select_active_at(s, InsuranceRate, 2099, 1)


def test_select_active_at_empty_table_returns_none(test_db_session):
    """整表無設定列（dev/測試/全新部署）→ 回 None，caller 沿用引擎預設（不 fail-loud）。"""
    s = test_db_session
    assert SalaryEngine._select_active_at(s, InsuranceRate, 2099, 1) is None
