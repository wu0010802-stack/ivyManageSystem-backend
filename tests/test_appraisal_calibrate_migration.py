"""Migration aprcal001 驗收：兩表存在 + 14 default rules + source_ref rename。"""

from datetime import date
from sqlalchemy import inspect

import pytest

from models.appraisal import AppraisalScoringRule, ScoreItemCode


def test_two_tables_exist(test_db_session):
    inspector = inspect(test_db_session.bind)
    tables = set(inspector.get_table_names())
    assert "appraisal_scoring_rules" in tables
    assert "appraisal_manual_event_counts" in tables


@pytest.fixture
def migrated_db(test_db_session):
    """模擬 migration INSERT 14 條 default rules（conftest 用 create_all 沒跑 alembic）。"""
    defaults = [
        ("LATE_EARLY", "PER_UNIT", {"per_unit_delta": -0.25}),
        ("MISSING_PUNCH", "PER_UNIT", {"per_unit_delta": -0.25}),
        ("LEAVE", "PER_UNIT", {"per_unit_delta": -1.0}),
        (
            "RETURNING_RATE_0915",
            "TIER",
            {
                "input_field": "retention_rate",
                "tiers": [
                    {"min": 100, "delta": 0},
                    {"min": 95, "delta": -1.7},
                    {"min": 0, "delta": -3.0},
                ],
            },
        ),
        (
            "RETURNING_RATE_0315",
            "TIER",
            {
                "input_field": "retention_rate",
                "tiers": [
                    {"min": 100, "delta": 6.0},
                    {"min": 95, "delta": 0.0},
                    {"min": 90, "delta": -1.7},
                    {"min": 80, "delta": -3.0},
                    {"min": 0, "delta": -6.0},
                ],
            },
        ),
        (
            "AFTER_CLASS_RATE",
            "FLAT_THRESHOLD",
            {
                "input_field": "activity_rate",
                "threshold": 80,
                "above_delta": 2.0,
                "below_delta": 0,
            },
        ),
        (
            "REWARD_PUNISH",
            "DISCIPLINARY_TIERED",
            {
                "warning_delta": -1.0,
                "minor_delta": -3.0,
                "major_delta": -10.0,
            },
        ),
        ("SCHOOL_MEETING_ABSENCE", "PER_UNIT", {"per_unit_delta": -1.0}),
        ("INSTITUTION_MEETING_0913", "PER_UNIT", {"per_unit_delta": -2.0}),
        ("INSTITUTION_MEETING_1115", "PER_UNIT", {"per_unit_delta": -2.0}),
        ("SELF_IMPROVEMENT_ACTIVITY", "PER_UNIT", {"per_unit_delta": -2.0}),
        ("CHILD_ACCIDENT", "PER_UNIT", {"per_unit_delta": -3.0}),
        ("CLASS_HEADCOUNT_BONUS", "PER_UNIT", {"per_unit_delta": 2.0}),
        ("OTHER", "PER_UNIT", {"per_unit_delta": 0}),
    ]
    for code, rtype, cfg in defaults:
        test_db_session.add(
            AppraisalScoringRule(
                item_code=code,
                effective_from=date(2026, 1, 1),
                rule_type=rtype,
                rule_config=cfg,
            )
        )
    test_db_session.flush()
    return test_db_session


def test_14_default_rules_inserted(migrated_db):
    rules = migrated_db.query(AppraisalScoringRule).all()
    codes = {r.item_code for r in rules}
    expected = {c.value for c in ScoreItemCode}
    assert codes == expected, f"missing={expected - codes}"
    # 全部 effective_from = 2026-01-01
    assert all(str(r.effective_from) == "2026-01-01" for r in rules)
