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


from schemas.year_end import OrgYearSettingsCreate, OrgYearSettingsOut


def test_create_schema_accepts_override():
    c = OrgYearSettingsCreate(
        semester_first=True,
        org_achievement_rate=Decimal("0"),
        school_achievement_rate_override=Decimal("91.5"),
    )
    assert c.school_achievement_rate_override == Decimal("91.5")


def test_create_schema_override_defaults_none():
    c = OrgYearSettingsCreate(semester_first=True, org_achievement_rate=Decimal("0"))
    assert c.school_achievement_rate_override is None


def test_out_schema_exposes_effective():
    o = OrgYearSettings(
        id=1,
        year_end_cycle_id=1,
        semester_first=True,
        enrollment_target=176,
        enrollment_actual=161,
        school_achievement_rate=Decimal("91.48"),
        school_achievement_rate_override=Decimal("91.5"),
        org_achievement_rate=Decimal("0"),
        meeting_absence_deduction=Decimal("1000"),
    )
    out = OrgYearSettingsOut.model_validate(o)
    assert out.school_achievement_rate_override == Decimal("91.5")
    assert out.effective_school_achievement_rate == Decimal("91.5")
