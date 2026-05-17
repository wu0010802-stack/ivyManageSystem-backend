"""appraisal_calibrate: scoring rules + manual event counts

Revision ID: aprcal001
Revises: f33ty9types, r4c3c0nd5n4p
Create Date: 2026-05-17
"""

from __future__ import annotations

import json
from datetime import date
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "aprcal001"
down_revision: Union[str, Sequence[str], None] = ("f33ty9types", "r4c3c0nd5n4p")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_RULES = [
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


def upgrade() -> None:
    # 1. 建 appraisal_scoring_rules
    op.create_table(
        "appraisal_scoring_rules",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("item_code", sa.String(64), nullable=False),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("rule_type", sa.String(32), nullable=False),
        sa.Column(
            "rule_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "applies_to_role_groups",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "item_code",
            "effective_from",
            name="uq_appraisal_scoring_rule_code_date",
        ),
    )
    op.create_index(
        "idx_scoring_rules_lookup",
        "appraisal_scoring_rules",
        ["item_code", sa.text("effective_from DESC")],
    )

    # 2. 建 appraisal_manual_event_counts
    op.create_table(
        "appraisal_manual_event_counts",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "cycle_id",
            sa.BigInteger,
            sa.ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "participant_id",
            sa.BigInteger,
            sa.ForeignKey("appraisal_participants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_code", sa.String(64), nullable=False),
        sa.Column(
            "count",
            sa.Numeric(8, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "entered_by",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "entered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("note", sa.Text, nullable=True),
        sa.UniqueConstraint(
            "cycle_id",
            "participant_id",
            "item_code",
            name="uq_appraisal_manual_event_count_triple",
        ),
    )
    op.create_index(
        "idx_manual_counts_cycle",
        "appraisal_manual_event_counts",
        ["cycle_id", "item_code"],
    )

    # 3. source_ref rename（舊 sync_score_items 4 條 → 新 lower_case item_code）
    conn = op.get_bind()
    conn.execute(text("""
            UPDATE appraisal_score_items
               SET source_ref = REPLACE(source_ref, 'auto:attendance:', 'auto:late_early:')
             WHERE source_ref LIKE 'auto:attendance:%'
            """))
    conn.execute(text("""
            UPDATE appraisal_score_items
               SET source_ref = REPLACE(source_ref, 'auto:returning_rate:', 'auto:returning_rate_0315:')
             WHERE source_ref LIKE 'auto:returning_rate:%'
            """))
    conn.execute(text("""
            UPDATE appraisal_score_items
               SET source_ref = REPLACE(source_ref, 'auto:after_class:', 'auto:after_class_rate:')
             WHERE source_ref LIKE 'auto:after_class:%'
            """))
    conn.execute(text("""
            UPDATE appraisal_score_items
               SET source_ref = REPLACE(source_ref, 'auto:disciplinary:', 'auto:reward_punish:')
             WHERE source_ref LIKE 'auto:disciplinary:%'
            """))

    # 4. 14 條 default rules INSERT
    for code, rtype, cfg in DEFAULT_RULES:
        conn.execute(
            text("""
                INSERT INTO appraisal_scoring_rules
                    (item_code, effective_from, rule_type, rule_config, notes)
                VALUES (:code, :ef, :rt, CAST(:cfg AS JSONB), :notes)
                """),
            {
                "code": code,
                "ef": date(2026, 1, 1),
                "rt": rtype,
                "cfg": json.dumps(cfg),
                "notes": "migration default — user 可在 UI 上覆寫新版",
            },
        )


def downgrade() -> None:
    # 1. 反向 source_ref rename
    conn = op.get_bind()
    conn.execute(text("""
            UPDATE appraisal_score_items
               SET source_ref = REPLACE(source_ref, 'auto:late_early:', 'auto:attendance:')
             WHERE source_ref LIKE 'auto:late_early:%'
            """))
    conn.execute(text("""
            UPDATE appraisal_score_items
               SET source_ref = REPLACE(source_ref, 'auto:returning_rate_0315:', 'auto:returning_rate:')
             WHERE source_ref LIKE 'auto:returning_rate_0315:%'
            """))
    conn.execute(text("""
            UPDATE appraisal_score_items
               SET source_ref = REPLACE(source_ref, 'auto:after_class_rate:', 'auto:after_class:')
             WHERE source_ref LIKE 'auto:after_class_rate:%'
            """))
    conn.execute(text("""
            UPDATE appraisal_score_items
               SET source_ref = REPLACE(source_ref, 'auto:reward_punish:', 'auto:disciplinary:')
             WHERE source_ref LIKE 'auto:reward_punish:%'
            """))

    op.drop_index("idx_manual_counts_cycle", table_name="appraisal_manual_event_counts")
    op.drop_table("appraisal_manual_event_counts")
    op.drop_index("idx_scoring_rules_lookup", table_name="appraisal_scoring_rules")
    op.drop_table("appraisal_scoring_rules")
