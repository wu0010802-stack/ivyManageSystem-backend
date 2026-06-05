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


# ─── Task 4：職位標準底薪 period-aware ───


def test_position_standards_resolved_by_year(test_db_session):
    from models.database import PositionSalaryConfig
    from services.salary.engine import load_position_salary_standards

    s = test_db_session
    s.add(PositionSalaryConfig(config_year=2026, version=1, head_teacher_a=39240))
    s.add(PositionSalaryConfig(config_year=2027, version=1, head_teacher_a=99999))
    s.flush()
    std = load_position_salary_standards(s, year=2026)
    assert std["head_teacher_a"] == 39240.0


def test_position_standards_no_year_keeps_latest(test_db_session):
    """year=None 維持舊行為（latest id desc），供年終 builder 等未遷移 caller 用。"""
    from models.database import PositionSalaryConfig
    from services.salary.engine import load_position_salary_standards

    s = test_db_session
    s.add(PositionSalaryConfig(config_year=2026, version=1, head_teacher_a=39240))
    s.add(PositionSalaryConfig(config_year=2027, version=1, head_teacher_a=99999))
    s.flush()
    std = load_position_salary_standards(s)  # 無 year
    assert std["head_teacher_a"] == 99999.0


def test_position_standards_empty_table_uses_defaults(test_db_session):
    """空表 + 指定 year → 用引擎預設（不 fail-loud；dev position_salary_configs 即空表）。"""
    from services.salary.engine import (
        load_position_salary_standards,
        _POSITION_SALARY_DEFAULTS,
    )

    s = test_db_session
    std = load_position_salary_standards(s, year=2099)
    assert std["head_teacher_a"] == float(_POSITION_SALARY_DEFAULTS["head_teacher_a"])


def test_position_standards_restored_after_config_for_month(test_db_session):
    """歷史月底薪不可洩漏到 baseline：離開 config_for_month 後須還原。"""
    from models.database import PositionSalaryConfig
    from services.salary.engine import SalaryEngine

    s = test_db_session
    s.add(PositionSalaryConfig(config_year=2026, version=1, head_teacher_a=39240))
    s.flush()
    engine = SalaryEngine(load_from_db=False)
    engine._position_salary_standards = {"head_teacher_a": 11111.0}  # baseline 哨兵
    with engine.config_for_month(s, 2026, 1):
        assert engine._position_salary_standards["head_teacher_a"] == 39240.0
    assert engine._position_salary_standards["head_teacher_a"] == 11111.0
