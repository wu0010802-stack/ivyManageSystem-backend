"""教師工作台（class-hub）後端測試。"""

from __future__ import annotations

import os
import sys
from datetime import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime

from services.portal_class_hub_service import (
    SLOT_DEFINITIONS,
    classify_time_to_slot,
    pick_sticky_next,
)


class TestClassifyTimeToSlot:
    @pytest.mark.parametrize(
        "hh_mm,expected",
        [
            ("06:30", "morning"),  # 早於早晨 → 落入 morning
            ("07:00", "morning"),
            ("08:59", "morning"),
            ("09:00", "forenoon"),
            ("11:30", "forenoon"),
            ("12:00", "noon"),
            ("13:30", "noon"),
            ("14:00", "afternoon"),
            ("17:30", "afternoon"),
            ("19:00", "afternoon"),  # 晚於下午 → 落入 afternoon
        ],
    )
    def test_classify(self, hh_mm: str, expected: str):
        h, m = map(int, hh_mm.split(":"))
        assert classify_time_to_slot(time(h, m)) == expected


class TestPickStickyNext:
    def test_returns_earliest_future(self):
        now = datetime(2026, 5, 3, 10, 0)
        cands = [
            {"due_at": datetime(2026, 5, 3, 9, 0), "name": "past"},
            {"due_at": datetime(2026, 5, 3, 11, 0), "name": "soon"},
            {"due_at": datetime(2026, 5, 3, 14, 0), "name": "later"},
        ]
        assert pick_sticky_next(cands, now)["name"] == "soon"

    def test_returns_none_when_all_past(self):
        now = datetime(2026, 5, 3, 18, 0)
        cands = [{"due_at": datetime(2026, 5, 3, 9, 0)}]
        assert pick_sticky_next(cands, now) is None

    def test_returns_none_when_empty(self):
        assert pick_sticky_next([], datetime(2026, 5, 3, 10, 0)) is None
