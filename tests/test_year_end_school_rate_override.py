"""Phase1: 全校達成率 HR 覆寫 — property / resolver / 端到端金額。"""

from __future__ import annotations
import os, sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from models.year_end import OrgYearSettings


def test_effective_rate_uses_auto_when_no_override():
    o = OrgYearSettings(
        school_achievement_rate=Decimal("91.48"),
        school_achievement_rate_override=None,
    )
    assert o.effective_school_achievement_rate == Decimal("91.48")


def test_effective_rate_uses_override_when_set():
    o = OrgYearSettings(
        school_achievement_rate=Decimal("91.48"),
        school_achievement_rate_override=Decimal("91.5"),
    )
    assert o.effective_school_achievement_rate == Decimal("91.5")
