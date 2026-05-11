"""get_minimum_wage(at_date) 查 DB / fallback / 邊界。"""

from datetime import date

import pytest

from models.database import MinimumWageHistory, session_scope
from services.salary.minimum_wage import (
    MINIMUM_HOURLY_WAGE,
    MINIMUM_MONTHLY_WAGE,
    get_minimum_wage,
    validate_minimum_wage,
)


@pytest.fixture
def seed_history(test_db_session):
    """確保 minimum_wage_history 有 2025/2026 兩筆 (bootstrap 在 alembic 才落地，
    Base.metadata.create_all 不會自動建，故測試手動 seed)。"""
    with session_scope() as s:
        for d, m, h in [
            (date(2025, 1, 1), 28590, 190),
            (date(2026, 1, 1), 29500, 196),
        ]:
            existing = s.query(MinimumWageHistory).filter_by(effective_date=d).first()
            if existing is None:
                s.add(
                    MinimumWageHistory(
                        effective_date=d,
                        monthly=m,
                        hourly=h,
                        confirmed_by="system",
                        confirm_reason="test seed minimum wage history",
                    )
                )
        s.flush()


def test_get_minimum_wage_uses_history_2026(seed_history):
    monthly, hourly = get_minimum_wage(date(2026, 6, 1))
    assert monthly == 29500
    assert hourly == 196


def test_get_minimum_wage_uses_history_2025(seed_history):
    monthly, hourly = get_minimum_wage(date(2025, 8, 1))
    assert monthly == 28590
    assert hourly == 190


def test_get_minimum_wage_after_2027_promote(seed_history):
    with session_scope() as s:
        s.add(
            MinimumWageHistory(
                effective_date=date(2027, 1, 1),
                monthly=30500,
                hourly=200,
                confirmed_by="admin",
                confirm_reason="政府公告 2027 基本工資調整",
            )
        )
        s.flush()
    monthly, hourly = get_minimum_wage(date(2027, 3, 1))
    assert monthly == 30500
    assert hourly == 200
    # 但 2026 仍取 2026 那筆
    monthly, hourly = get_minimum_wage(date(2026, 12, 31))
    assert monthly == 29500


def test_get_minimum_wage_fallback_on_db_failure(monkeypatch, seed_history):
    """DB 查詢拋例外時回退常數。"""
    from services.salary import minimum_wage as mw_module

    def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(mw_module, "_query_history", boom)
    monthly, hourly = get_minimum_wage(date(2026, 6, 1))
    assert monthly == MINIMUM_MONTHLY_WAGE
    assert hourly == MINIMUM_HOURLY_WAGE


def test_validate_minimum_wage_uses_db(seed_history):
    """validate_minimum_wage 改用 get_minimum_wage(today)，不再讀常數。"""
    from fastapi import HTTPException

    # 邊界：2026 法定 29500；29000 應 raise
    with pytest.raises(HTTPException) as exc:
        validate_minimum_wage(employee_type="regular", base_salary=29000, hourly_rate=0)
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "BELOW_MINIMUM_WAGE"

    # 邊界：剛好 = 法定值不應 raise
    validate_minimum_wage(employee_type="regular", base_salary=29500, hourly_rate=0)
