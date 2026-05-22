"""Smoke test: scheduler module imports cleanly, today_taipei returns date,
scheduler_enabled is bool."""

from datetime import date
from services.recruitment_term_advance_scheduler import (
    _today_taipei,
    scheduler_enabled,
    run_recruitment_term_advance_scheduler,
)


def test_today_taipei_returns_date():
    assert isinstance(_today_taipei(), date)


def test_scheduler_enabled_returns_bool():
    assert isinstance(scheduler_enabled(), bool)


def test_run_scheduler_is_coroutine():
    import asyncio

    assert asyncio.iscoroutinefunction(run_recruitment_term_advance_scheduler)
