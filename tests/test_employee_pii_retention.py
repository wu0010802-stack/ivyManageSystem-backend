"""Employee 5y retention GC scheduler step."""

from datetime import date, datetime, timedelta, timezone

import pytest


def test_employee_retention_years_default() -> None:
    from services.pii_retention_scheduler import employee_retention_years

    assert employee_retention_years() == 5


def test_employee_retention_years_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMPLOYEE_PII_RETENTION_YEARS", "3")
    # reset settings cache
    from config import get_settings

    get_settings.cache_clear()
    from services.pii_retention_scheduler import employee_retention_years

    try:
        assert employee_retention_years() == 3
    finally:
        get_settings.cache_clear()


def test_employee_pii_gc_smoke_function_exists() -> None:
    """smoke test：function 存在且可 import；實際 DB 行為由 e2e 驗"""
    from services.pii_retention_scheduler import _run_employee_pii_retention_gc

    assert callable(_run_employee_pii_retention_gc)
