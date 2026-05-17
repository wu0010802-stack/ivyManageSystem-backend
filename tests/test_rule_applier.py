"""rule_applier.py 純函式測試（不接 DB）。"""

from datetime import date
from decimal import Decimal

import pytest

from models.appraisal import RoleGroup, ScoreItemCode
from services.appraisal.rule_applier import (
    ScoringRule,
    apply_per_unit,
)


def _rule(rule_type, config, item_code=ScoreItemCode.LATE_EARLY):
    return ScoringRule(
        item_code=item_code.value,
        effective_from=date(2026, 1, 1),
        rule_type=rule_type,
        rule_config=config,
        applies_to_role_groups=None,
    )


class TestApplyPerUnit:
    def test_basic_count_times_delta(self):
        rule = _rule("PER_UNIT", {"per_unit_delta": -0.25})
        assert apply_per_unit(rule, Decimal("4"), RoleGroup.HEAD_TEACHER) == Decimal(
            "-1.00"
        )

    def test_per_role_override(self):
        rule = _rule(
            "PER_UNIT",
            {
                "per_unit_delta": -0.25,
                "per_role_override": {"ASSISTANT": -0.5},
            },
        )
        assert apply_per_unit(rule, Decimal("4"), RoleGroup.ASSISTANT) == Decimal(
            "-2.00"
        )
        assert apply_per_unit(rule, Decimal("4"), RoleGroup.HEAD_TEACHER) == Decimal(
            "-1.00"
        )

    def test_unit_cap_clamps_count(self):
        rule = _rule("PER_UNIT", {"per_unit_delta": -0.25, "unit_cap": 10})
        # count=20 → 套 cap=10 → 10 × -0.25 = -2.5
        assert apply_per_unit(rule, Decimal("20"), RoleGroup.HEAD_TEACHER) == Decimal(
            "-2.50"
        )

    def test_delta_cap_clamps_result(self):
        rule = _rule("PER_UNIT", {"per_unit_delta": -1, "delta_cap": -5})
        # count=10 → -10 → 但 delta_cap=-5 → 最終 -5
        assert apply_per_unit(rule, Decimal("10"), RoleGroup.HEAD_TEACHER) == Decimal(
            "-5.00"
        )
