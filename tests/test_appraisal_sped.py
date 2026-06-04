"""SPED 特教生手填加分：count × +2，歸 MANUAL。"""

from datetime import date
from decimal import Decimal

import pytest

from models.appraisal import ScoreItemCode, AUTO_ITEM_CODES, MANUAL_ITEM_CODES
from services.appraisal.rule_applier import ScoringRule, RoleGroup, apply_per_unit


def test_sped_in_enum_and_manual():
    assert ScoreItemCode.SPED.value == "SPED"
    assert ScoreItemCode.SPED in MANUAL_ITEM_CODES
    assert ScoreItemCode.SPED not in AUTO_ITEM_CODES


def test_sped_per_unit_2_count_2_yields_4():
    """SPED PER_UNIT rule：2 位特教生 × +2 = +4.0（對齊 Excel P 欄）。"""
    rule = ScoringRule(
        item_code="SPED",
        effective_from=date(2025, 8, 1),
        rule_type="PER_UNIT",
        rule_config={"per_unit_delta": 2.0},
        applies_to_role_groups=None,
    )
    result = apply_per_unit(rule, Decimal("2"), RoleGroup.HEAD_TEACHER)
    assert result == Decimal("4.00")
