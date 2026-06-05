"""Task 5：load_brackets_from_db(require_year=True) 的 fail-loud 行為。

語意（與其他設定的空表 fallback 一致）：
- 表有級距但缺該年度 → raise PayrollConfigMissingError（行政漏建該年度級距）。
- 整表完全無級距 → 不 raise，沿用 hardcode（dev / 全新部署）。
"""

import pytest

from models.database import InsuranceBracket
from services.insurance_service import InsuranceService
from services.salary.config_resolver import PayrollConfigMissingError


def _seed_bracket(s, year):
    s.add(
        InsuranceBracket(
            effective_year=year,
            amount=27470,
            labor_employee=1,
            labor_employer=2,
            health_employee=3,
            health_employer=4,
            pension=5,
        )
    )


def test_require_year_raises_when_populated_but_year_missing(test_db_session):
    """表有 2026 級距、要求 2099 → fail-loud。"""
    _seed_bracket(test_db_session, 2026)
    test_db_session.commit()  # 跨 session 可見（service 自開 session）
    svc = InsuranceService()
    with pytest.raises(PayrollConfigMissingError):
        svc.load_brackets_from_db(2099, require_year=True)


def test_require_year_empty_table_falls_back_to_hardcode(test_db_session):
    """整表無級距 + require_year → 不 raise，回 False（沿用 hardcode）。"""
    svc = InsuranceService()
    original = svc.table
    result = svc.load_brackets_from_db(2099, require_year=True)
    assert result is False
    assert svc.table is original  # 未被覆寫，仍 hardcode


def test_require_year_false_keeps_silent_fallback(test_db_session):
    """require_year=False（baseline/startup）：缺年度時維持既有 silent fallback，不 raise。"""
    _seed_bracket(test_db_session, 2026)
    test_db_session.commit()
    svc = InsuranceService()
    # 2099 無資料但有 2026（<=2099）→ 既有 fallback 撿 2026，回 True、不 raise
    result = svc.load_brackets_from_db(2099, require_year=False)
    assert result is True
