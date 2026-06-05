"""R2：年終 BonusConfig 依「民國學年 → 西元 config_year = N+1911」解析。

驗證 bonus_config_for_academic_year 與 festival_base_for_role(academic_year=) 的年度解析
+ 空表 fallback（與薪資引擎一致：整表空→None/預設、有料缺年度→fail-loud）。
"""

from decimal import Decimal

import pytest

from models.config import BonusConfig
from services.salary.config_resolver import PayrollConfigMissingError
from services.year_end import settlement_builder as sb


def test_resolves_academic_year_plus_1911(test_db_session):
    """學年 114 → 解析 config_year 2025（=114+1911），即使 2026 也存在也不誤撿。"""
    s = test_db_session
    s.add(BonusConfig(config_year=2025, version=1, head_teacher_ab=2000))
    s.add(BonusConfig(config_year=2026, version=1, head_teacher_ab=9999))
    s.flush()
    cfg = sb.bonus_config_for_academic_year(s, 114)
    assert cfg.config_year == 2025
    assert cfg.head_teacher_ab == 2000


def test_empty_table_returns_none(test_db_session):
    """bonus_configs 整表空 → None（caller 沿用內建預設，不 fail-loud）。"""
    s = test_db_session
    assert sb.bonus_config_for_academic_year(s, 114) is None


def test_populated_but_missing_year_fail_loud(test_db_session):
    """表有 2025、求學年 115（→2026）缺 → fail-loud。"""
    s = test_db_session
    s.add(BonusConfig(config_year=2025, version=1, head_teacher_ab=2000))
    s.flush()
    with pytest.raises(PayrollConfigMissingError):
        sb.bonus_config_for_academic_year(s, 115)  # 115+1911=2026 缺


def test_festival_base_for_role_uses_year_mapping(test_db_session):
    """festival_base_for_role(academic_year=114) 取 config_year 2025 的角色基數。"""
    s = test_db_session
    s.add(BonusConfig(config_year=2025, version=1, head_teacher_ab=2000))
    s.add(BonusConfig(config_year=2026, version=1, head_teacher_ab=9999))
    s.flush()
    assert sb.festival_base_for_role(s, "head_teacher_ab", 114) == Decimal("2000")


def test_festival_base_for_role_none_year_keeps_latest(test_db_session):
    """academic_year=None → 維持舊行為（latest is_active），供未遷移 caller/測試。"""
    s = test_db_session
    s.add(BonusConfig(config_year=2025, version=1, head_teacher_ab=2000))
    s.add(BonusConfig(config_year=2026, version=1, head_teacher_ab=9999))
    s.flush()
    assert sb.festival_base_for_role(s, "head_teacher_ab") == Decimal("9999")
