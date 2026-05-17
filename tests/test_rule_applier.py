"""rule_applier.py 純函式測試（不接 DB）。"""

from datetime import date
from decimal import Decimal

import pytest

from models.appraisal import RoleGroup, ScoreItemCode
from services.appraisal.rule_applier import (
    ScoringRule,
    apply_disciplinary_tiered,
    apply_flat_threshold,
    apply_per_unit,
    apply_tier,
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


class TestApplyTier:
    def _make_rule(self, tiers):
        return _rule("TIER", {"input_field": "retention_rate", "tiers": tiers})

    def test_value_at_tier_boundary(self):
        rule = self._make_rule(
            [
                {"min": 100, "delta": 6},
                {"min": 95, "delta": 0},
                {"min": 0, "delta": -6},
            ]
        )
        assert apply_tier(rule, Decimal("100"), RoleGroup.HEAD_TEACHER) == Decimal(
            "6.00"
        )
        assert apply_tier(rule, Decimal("95"), RoleGroup.HEAD_TEACHER) == Decimal(
            "0.00"
        )

    def test_value_between_tiers_falls_to_lower(self):
        rule = self._make_rule(
            [
                {"min": 100, "delta": 6},
                {"min": 95, "delta": 0},
                {"min": 0, "delta": -6},
            ]
        )
        assert apply_tier(rule, Decimal("97"), RoleGroup.HEAD_TEACHER) == Decimal(
            "0.00"
        )
        assert apply_tier(rule, Decimal("50"), RoleGroup.HEAD_TEACHER) == Decimal(
            "-6.00"
        )

    def test_min_zero_catch_all(self):
        rule = self._make_rule(
            [
                {"min": 50, "delta": 2},
                {"min": 0, "delta": -5},
            ]
        )
        assert apply_tier(rule, Decimal("0"), RoleGroup.HEAD_TEACHER) == Decimal(
            "-5.00"
        )

    def test_unsorted_tiers_handled(self):
        rule = self._make_rule(
            [
                {"min": 0, "delta": -6},
                {"min": 100, "delta": 6},
                {"min": 95, "delta": 0},
            ]
        )
        assert apply_tier(rule, Decimal("100"), RoleGroup.HEAD_TEACHER) == Decimal(
            "6.00"
        )


class TestApplyFlatThreshold:
    def _make_rule(self, threshold, above, below):
        return _rule(
            "FLAT_THRESHOLD",
            {
                "input_field": "activity_rate",
                "threshold": threshold,
                "above_delta": above,
                "below_delta": below,
            },
        )

    def test_value_above_threshold(self):
        rule = self._make_rule(80, 2, 0)
        assert apply_flat_threshold(
            rule, Decimal("90"), RoleGroup.HEAD_TEACHER
        ) == Decimal("2.00")

    def test_value_below_threshold(self):
        rule = self._make_rule(80, 2, -1)
        assert apply_flat_threshold(
            rule, Decimal("70"), RoleGroup.HEAD_TEACHER
        ) == Decimal("-1.00")

    def test_value_equal_threshold_counts_as_above(self):
        rule = self._make_rule(80, 2, 0)
        assert apply_flat_threshold(
            rule, Decimal("80"), RoleGroup.HEAD_TEACHER
        ) == Decimal("2.00")


class TestApplyDisciplinaryTiered:
    def _make_rule(self):
        return _rule(
            "DISCIPLINARY_TIERED",
            {
                "warning_delta": -1,
                "minor_delta": -3,
                "major_delta": -10,
            },
            item_code=ScoreItemCode.REWARD_PUNISH,
        )

    def test_all_zero(self):
        assert apply_disciplinary_tiered(self._make_rule(), 0, 0, 0) == Decimal("0.00")

    def test_basic_sum(self):
        # 2*-1 + 1*-3 + 0*-10 = -5
        assert apply_disciplinary_tiered(self._make_rule(), 2, 1, 0) == Decimal("-5.00")

    def test_major_only(self):
        # 2*-10 = -20
        assert apply_disciplinary_tiered(self._make_rule(), 0, 0, 2) == Decimal(
            "-20.00"
        )

    def test_mixed(self):
        # -1 + -3 + -10 = -14
        assert apply_disciplinary_tiered(self._make_rule(), 1, 1, 1) == Decimal(
            "-14.00"
        )
