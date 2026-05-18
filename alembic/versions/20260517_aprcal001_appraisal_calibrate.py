"""appraisal_calibrate: scoring rules + manual event counts

Revision ID: aprcal001
Revises: f33ty9types, r4c3c0nd5n4p
Create Date: 2026-05-17

bug sweep 2026-05-18 P2 文件化（transaction 邊界）：
    本檔 upgrade() 內所有 DDL (create_table / create_index) 與 DML
    (UPDATE source_ref / UPDATE item_code rename / INSERT DEFAULT_RULES) 都包在
    alembic 預設的「每 migration 一個 transaction」內（env.py 採 transaction_per_migration
    + transactional_ddl 為 True for PostgreSQL）。意義：任一 step 拋例外，整個
    migration rollback，不會留下「table 建好但 default rules 沒灌進去」或「source_ref
    rename 完但 item_code rename 失敗」這類 partial state；可放心 retry。
    若未來改成單 migration 內跨多個 conn 或自行 BEGIN/COMMIT，要重新檢查此假設。
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


# bug sweep 2026-05-18 P1-4：班級績效類規則（才藝率/留校率）僅教學職適用。
# COOK / SUPERVISOR / STAFF 沒有自己的班級，套這些規則只會吃到 0% → -3 ~ -6
# 的虛假扣分。改為 applies_to_role_groups=['HEAD_TEACHER','ASSISTANT']
# 對非教學職跳過。
_TEACHING_ROLES = ["HEAD_TEACHER", "ASSISTANT"]

DEFAULT_RULES = [
    ("LATE_EARLY", "PER_UNIT", {"per_unit_delta": -0.25}, None),
    ("MISSING_PUNCH", "PER_UNIT", {"per_unit_delta": -0.25}, None),
    ("LEAVE", "PER_UNIT", {"per_unit_delta": -1.0}, None),
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
        _TEACHING_ROLES,
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
        _TEACHING_ROLES,
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
        _TEACHING_ROLES,
    ),
    (
        "REWARD_PUNISH",
        "DISCIPLINARY_TIERED",
        {
            "warning_delta": -1.0,
            "minor_delta": -3.0,
            "major_delta": -10.0,
        },
        None,
    ),
    ("SCHOOL_MEETING_ABSENCE", "PER_UNIT", {"per_unit_delta": -1.0}, None),
    ("INSTITUTION_MEETING_0913", "PER_UNIT", {"per_unit_delta": -2.0}, None),
    ("INSTITUTION_MEETING_1115", "PER_UNIT", {"per_unit_delta": -2.0}, None),
    ("SELF_IMPROVEMENT_ACTIVITY", "PER_UNIT", {"per_unit_delta": -2.0}, None),
    ("CHILD_ACCIDENT", "PER_UNIT", {"per_unit_delta": -3.0}, None),
    ("CLASS_HEADCOUNT_BONUS", "PER_UNIT", {"per_unit_delta": 2.0}, None),
    ("OTHER", "PER_UNIT", {"per_unit_delta": 0}, None),
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

    # 3b. P1-2 defensive cleanup：item_code 欄位舊小寫值對應到新 14-code enum。
    # 1bcb251f 之前的 _AUTO_SOURCE_TYPE_TO_ITEM_CODE 顯示 sync_score_items
    # 寫入的 item_code 一直是 enum 大寫值（如 'LATE_EARLY'），未真的出現
    # 小寫舊值。但本批次仍保留防禦性 UPDATE，覆蓋以下情境：
    #   (a) 早期 dev 環境曾有人 raw SQL INSERT 進測試資料
    #   (b) 未來 reseed/import 走旁路時的 fail-safe
    # prod 實測無相符 row，這段 UPDATE affected_rows 預期為 0。
    item_code_rename = [
        ("attendance", "LATE_EARLY"),
        ("returning_rate", "RETURNING_RATE_0315"),
        ("after_class", "AFTER_CLASS_RATE"),
        ("disciplinary", "REWARD_PUNISH"),
    ]
    for old, new in item_code_rename:
        conn.execute(
            text(
                "UPDATE appraisal_score_items SET item_code = :new "
                "WHERE item_code = :old"
            ),
            {"old": old, "new": new},
        )

    # 4. 14 條 default rules INSERT（含 applies_to_role_groups）
    for code, rtype, cfg, role_groups in DEFAULT_RULES:
        conn.execute(
            text("""
                INSERT INTO appraisal_scoring_rules
                    (item_code, effective_from, rule_type, rule_config,
                     applies_to_role_groups, notes)
                VALUES (:code, :ef, :rt, CAST(:cfg AS JSONB),
                        CAST(:rg AS JSONB), :notes)
                """),
            {
                "code": code,
                "ef": date(2026, 1, 1),
                "rt": rtype,
                "cfg": json.dumps(cfg),
                "rg": json.dumps(role_groups) if role_groups is not None else None,
                "notes": "migration default — user 可在 UI 上覆寫新版",
            },
        )

    # 5. P1-4 defensive data migration：把舊版「無 role 限制」的班級績效
    # 規則（既存 prod row 若 applies_to_role_groups IS NULL）補上限制。
    # 若 prod 在 1bcb251f 之後就已套這支 migration 的原版（無 role 限制），
    # 這段 UPDATE 會把那些 row 帶上 role 限制；若 prod 已是新版，UPDATE
    # 不會動到任何 row。
    conn.execute(
        text("""
            UPDATE appraisal_scoring_rules
               SET applies_to_role_groups = CAST(:rg AS JSONB)
             WHERE item_code IN (
                'AFTER_CLASS_RATE',
                'RETURNING_RATE_0315',
                'RETURNING_RATE_0915'
             )
               AND applies_to_role_groups IS NULL
            """),
        {"rg": json.dumps(_TEACHING_ROLES)},
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
    # P1-24：post-migration sync 會寫 _0915 source_ref（item_code RETURNING_RATE_0915
    # 對應）；downgrade 也必須還原回舊單一 `auto:returning_rate:`，否則 prod 緊急
    # downgrade 後 _0915 rows 會變孤兒、舊代碼吃不到。
    conn.execute(text("""
            UPDATE appraisal_score_items
               SET source_ref = REPLACE(source_ref, 'auto:returning_rate_0915:', 'auto:returning_rate:')
             WHERE source_ref LIKE 'auto:returning_rate_0915:%'
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
