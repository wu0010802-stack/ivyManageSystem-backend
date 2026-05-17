"""rule_applier.py 純函式測試（不接 DB）。"""

from datetime import date
from decimal import Decimal

import pytest

from models.appraisal import (
    AppraisalCycle,
    AppraisalManualEventCount,
    AppraisalParticipant,
    AppraisalScoringRule,
    CycleStatus,
    RoleGroup,
    ScoreItemCode,
    Semester,
)
from services.appraisal.rule_applier import (
    ScoringRule,
    apply_disciplinary_tiered,
    apply_flat_threshold,
    apply_per_unit,
    apply_tier,
    compute_all_deltas,
    load_rules_for_date,
    rule_applies_to_role,
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


class TestLoadRulesForDate:
    def test_picks_latest_version_before_on_date(self, test_db_session):
        s = test_db_session
        s.add_all(
            [
                AppraisalScoringRule(
                    item_code="LATE_EARLY",
                    effective_from=date(2026, 1, 1),
                    rule_type="PER_UNIT",
                    rule_config={"per_unit_delta": -0.25},
                ),
                AppraisalScoringRule(
                    item_code="LATE_EARLY",
                    effective_from=date(2026, 7, 1),
                    rule_type="PER_UNIT",
                    rule_config={"per_unit_delta": -0.5},
                ),
            ]
        )
        s.flush()
        rules = load_rules_for_date(s, date(2026, 6, 1))
        assert rules["LATE_EARLY"].rule_config["per_unit_delta"] == -0.25
        rules2 = load_rules_for_date(s, date(2026, 8, 1))
        assert rules2["LATE_EARLY"].rule_config["per_unit_delta"] == -0.5

    def test_skips_future_versions(self, test_db_session):
        s = test_db_session
        s.add(
            AppraisalScoringRule(
                item_code="LATE_EARLY",
                effective_from=date(2027, 1, 1),
                rule_type="PER_UNIT",
                rule_config={"per_unit_delta": -0.25},
            )
        )
        s.flush()
        rules = load_rules_for_date(s, date(2026, 6, 1))
        assert "LATE_EARLY" not in rules

    def test_empty_returns_empty_dict(self, test_db_session):
        rules = load_rules_for_date(test_db_session, date(2026, 6, 1))
        assert rules == {}

    def test_loads_all_item_codes(self, test_db_session):
        s = test_db_session
        for code in ("LATE_EARLY", "MISSING_PUNCH", "LEAVE"):
            s.add(
                AppraisalScoringRule(
                    item_code=code,
                    effective_from=date(2026, 1, 1),
                    rule_type="PER_UNIT",
                    rule_config={"per_unit_delta": -0.25},
                )
            )
        s.flush()
        rules = load_rules_for_date(s, date(2026, 6, 1))
        assert set(rules.keys()) == {"LATE_EARLY", "MISSING_PUNCH", "LEAVE"}


class TestAppliesToRoleGroupsFilter:
    def test_none_means_all(self):
        rule = ScoringRule(
            item_code="LATE_EARLY",
            effective_from=date(2026, 1, 1),
            rule_type="PER_UNIT",
            rule_config={"per_unit_delta": -0.25},
            applies_to_role_groups=None,
        )
        assert rule_applies_to_role(rule, RoleGroup.SUPERVISOR) is True
        assert rule_applies_to_role(rule, RoleGroup.HEAD_TEACHER) is True

    def test_list_filters(self):
        rule = ScoringRule(
            item_code="RETURNING_RATE_0315",
            effective_from=date(2026, 1, 1),
            rule_type="TIER",
            rule_config={
                "input_field": "retention_rate",
                "tiers": [{"min": 0, "delta": 0}],
            },
            applies_to_role_groups=["HEAD_TEACHER", "ASSISTANT"],
        )
        assert rule_applies_to_role(rule, RoleGroup.HEAD_TEACHER) is True
        assert rule_applies_to_role(rule, RoleGroup.SUPERVISOR) is False


def test_compute_all_deltas_smoke(test_db_session, monkeypatch):
    """整合測試：5 auto + 9 manual 全 14 條對單一 participant 算出 delta。"""
    s = test_db_session
    # 1. 建 cycle + participant
    cycle = AppraisalCycle(
        academic_year=114,
        semester=Semester.FIRST,
        start_date=date(2025, 8, 1),
        end_date=date(2026, 1, 31),
        base_score_calc_date=date(2025, 9, 15),
        base_score=Decimal("75.6"),
        status=CycleStatus.OPEN,
    )
    s.add(cycle)
    s.flush()
    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=1,
        role_group=RoleGroup.HEAD_TEACHER,
        hire_months_in_cycle=Decimal("6"),
        is_excluded=False,
    )
    s.add(p)
    s.flush()

    # 14 default rules
    defaults = [
        ("LATE_EARLY", "PER_UNIT", {"per_unit_delta": -0.25}),
        ("MISSING_PUNCH", "PER_UNIT", {"per_unit_delta": -0.25}),
        ("LEAVE", "PER_UNIT", {"per_unit_delta": -1.0}),
        (
            "RETURNING_RATE_0915",
            "TIER",
            {"input_field": "retention_rate", "tiers": [{"min": 0, "delta": 0}]},
        ),
        (
            "RETURNING_RATE_0315",
            "TIER",
            {
                "input_field": "retention_rate",
                "tiers": [
                    {"min": 100, "delta": 6},
                    {"min": 0, "delta": -6},
                ],
            },
        ),
        (
            "AFTER_CLASS_RATE",
            "FLAT_THRESHOLD",
            {
                "input_field": "activity_rate",
                "threshold": 80,
                "above_delta": 2,
                "below_delta": 0,
            },
        ),
        (
            "REWARD_PUNISH",
            "DISCIPLINARY_TIERED",
            {"warning_delta": -1, "minor_delta": -3, "major_delta": -10},
        ),
        ("SCHOOL_MEETING_ABSENCE", "PER_UNIT", {"per_unit_delta": -1}),
        ("INSTITUTION_MEETING_0913", "PER_UNIT", {"per_unit_delta": -2}),
        ("INSTITUTION_MEETING_1115", "PER_UNIT", {"per_unit_delta": -2}),
        ("SELF_IMPROVEMENT_ACTIVITY", "PER_UNIT", {"per_unit_delta": -2}),
        ("CHILD_ACCIDENT", "PER_UNIT", {"per_unit_delta": -3}),
        ("CLASS_HEADCOUNT_BONUS", "PER_UNIT", {"per_unit_delta": 2}),
        ("OTHER", "PER_UNIT", {"per_unit_delta": 0}),
    ]
    for code, rt, cfg in defaults:
        s.add(
            AppraisalScoringRule(
                item_code=code,
                effective_from=date(2025, 1, 1),
                rule_type=rt,
                rule_config=cfg,
            )
        )
    # 9 manual counts（部分填）
    for code in ("SCHOOL_MEETING_ABSENCE", "INSTITUTION_MEETING_0913"):
        s.add(
            AppraisalManualEventCount(
                cycle_id=cycle.id,
                participant_id=p.id,
                item_code=code,
                count=Decimal("1"),
            )
        )
    s.flush()

    # 2. mock aggregate_cycle_status
    from services.appraisal import status_aggregator as agg
    from services.appraisal.status_aggregator import (
        ActivityRateAggregate,
        AttendanceAggregate,
        ClassRetentionAggregate,
        DisciplinaryAggregate,
        ParticipantStatus,
    )

    fake_status = ParticipantStatus(
        participant_id=p.id,
        employee_id=1,
        employee_name="王雅玲",
        role_group=RoleGroup.HEAD_TEACHER.value,
        classroom_id=10,
        attendance=AttendanceAggregate(
            employee_id=1,
            late_count=3,
            early_leave_count=0,
            missing_punch_count=3,
            leave_days=1,
            suggested_score_delta=Decimal("0"),
        ),
        retention=ClassRetentionAggregate(
            employee_id=1,
            classroom_id=10,
            classroom_name="A",
            initial_count=20,
            final_count=20,
            retention_rate=Decimal("100"),
            suggested_score_delta=Decimal("0"),
        ),
        activity=ActivityRateAggregate(
            employee_id=1,
            classroom_id=10,
            enrolled_students=20,
            registered_for_activity=18,
            activity_rate=Decimal("90"),
            suggested_score_delta=Decimal("0"),
        ),
        disciplinary=DisciplinaryAggregate(
            employee_id=1,
            warning_count=1,
            minor_count=0,
            major_count=0,
            actions=[],
            suggested_score_delta=Decimal("0"),
        ),
        is_participant=True,
        hire_months_in_cycle=Decimal("6"),
    )
    monkeypatch.setattr(agg, "aggregate_cycle_status", lambda session, c: [fake_status])

    # 3. 呼叫
    result = compute_all_deltas(s, cycle)

    # 4. 驗：14 條 item_code 都有結果
    keys = [code for (pid, code), _dr in result.items() if pid == p.id]
    assert len(keys) == 14
    late = result[(p.id, "LATE_EARLY")]
    assert late.delta == Decimal("-0.75")
    miss = result[(p.id, "MISSING_PUNCH")]
    assert miss.delta == Decimal("-0.75")
    ret = result[(p.id, "RETURNING_RATE_0315")]
    assert ret.delta == Decimal("6.00")
    act = result[(p.id, "AFTER_CLASS_RATE")]
    assert act.delta == Decimal("2.00")
    rp = result[(p.id, "REWARD_PUNISH")]
    assert rp.delta == Decimal("-1.00")
    smt = result[(p.id, "SCHOOL_MEETING_ABSENCE")]
    assert smt.delta == Decimal("-1.00")
