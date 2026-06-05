import pytest

from models.database import BonusConfig, InsuranceRate, InsuranceBracket
from services.salary.config_resolver import (
    resolve_config,
    resolve_brackets,
    PayrollConfigMissingError,
)


def test_resolve_config_picks_requested_year_not_latest(test_db_session):
    """頭號 bug：DB 同時有 2026/2027，查 2026 必須回 2026（不是最新的 2027）。"""
    s = test_db_session
    s.add(BonusConfig(config_year=2026, version=1, head_teacher_ab=2000))
    s.add(BonusConfig(config_year=2027, version=1, head_teacher_ab=9999))
    s.flush()
    row = resolve_config(s, BonusConfig, 2026, year_col="config_year")
    assert row.config_year == 2026
    assert row.head_teacher_ab == 2000


def test_resolve_config_picks_highest_version_within_year(test_db_session):
    s = test_db_session
    s.add(BonusConfig(config_year=2026, version=1, head_teacher_ab=2000))
    s.add(BonusConfig(config_year=2026, version=2, head_teacher_ab=2500))
    s.flush()
    row = resolve_config(s, BonusConfig, 2026, year_col="config_year")
    assert row.version == 2
    assert row.head_teacher_ab == 2500


def test_resolve_config_missing_year_raises(test_db_session):
    s = test_db_session
    with pytest.raises(PayrollConfigMissingError) as exc:
        resolve_config(s, BonusConfig, 2099, year_col="config_year")
    assert exc.value.year == 2099
    assert exc.value.config_type == "BonusConfig"


def test_resolve_config_works_for_insurance_rate(test_db_session):
    s = test_db_session
    s.add(InsuranceRate(rate_year=2026, version=1, supplementary_health_rate=0.0211))
    s.flush()
    row = resolve_config(s, InsuranceRate, 2026, year_col="rate_year")
    assert row.rate_year == 2026


def test_resolve_brackets_returns_all_rows_for_year(test_db_session):
    s = test_db_session
    s.add(
        InsuranceBracket(
            effective_year=2026,
            amount=27470,
            labor_employee=1,
            labor_employer=2,
            health_employee=3,
            health_employer=4,
            pension=5,
        )
    )
    s.add(
        InsuranceBracket(
            effective_year=2026,
            amount=28800,
            labor_employee=1,
            labor_employer=2,
            health_employee=3,
            health_employer=4,
            pension=5,
        )
    )
    s.flush()
    rows = resolve_brackets(s, 2026)
    assert len(rows) == 2
    assert rows[0]["amount"] <= rows[1]["amount"]


def test_resolve_brackets_missing_year_raises(test_db_session):
    s = test_db_session
    with pytest.raises(PayrollConfigMissingError):
        resolve_brackets(s, 2099)
