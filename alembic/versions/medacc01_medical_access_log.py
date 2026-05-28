"""P0d-2 medical_access_log

Revision ID: medacc01
Revises: mergeheads06
Create Date: 2026-05-28

P0 法規/個資 sprint 第四件 Phase 2：個資法 §6 特種個資取用稽核。

新增 medical_access_log 表（不與 audit_log 混，因為醫療取用屬獨立稽核 trail）：
- 每筆 = 一次醫療欄位讀取
- 含 user_id / student_id / field_name / reason / accessed_at / ip
- reason 不可為空（DB 層 nullable=False；endpoint 層 ≥10 字 gate）

Refs: docs/superpowers/specs/2026-05-28-medical-fields-encryption-design.md §3.4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "medacc01"
down_revision = "mergeheads06"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "medical_access_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
            comment="取用者 user_id（離職員工 deleted 後可變 NULL 保留稽核軌跡）",
        ),
        sa.Column(
            "student_id",
            sa.Integer,
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "field_name",
            sa.String(50),
            nullable=False,
            comment="allergy / medication / special_needs / temperature_c / bundle",
        ),
        sa.Column(
            "reason",
            sa.Text,
            nullable=False,
            comment="取用理由（endpoint 層 ≥10 字 gate）",
        ),
        sa.Column(
            "accessed_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("ip_address", sa.String(45), nullable=True),
    )
    op.create_index(
        "ix_mal_student_field_time",
        "medical_access_log",
        ["student_id", "field_name", "accessed_at"],
    )


def downgrade():
    op.drop_index("ix_mal_student_field_time", table_name="medical_access_log")
    op.drop_table("medical_access_log")
