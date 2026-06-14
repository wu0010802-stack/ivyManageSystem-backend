"""SeedContext registry 與月份委派的單元測試(純邏輯,不連 DB)。"""

from __future__ import annotations

import random

from scripts.seedgen.config import SeedConfig
from scripts.seedgen.context import SeedContext


def _make_ctx() -> SeedContext:
    return SeedContext(
        session=None,
        config=SeedConfig(),
        rng=random.Random(1),
    )


def test_log_accumulates():
    ctx = _make_ctx()
    ctx.log("students", 5)
    ctx.log("students", 3)
    assert ctx.counts["students"] == 8


def test_closed_months_delegation():
    ctx = _make_ctx()
    closed = ctx.closed_months()
    assert closed[0] == (2025, 8)
    assert (2026, 2) not in closed


def test_current_month_delegation():
    ctx = _make_ctx()
    assert ctx.current_month() == (2026, 2)
