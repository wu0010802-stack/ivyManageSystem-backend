"""class_enrollment_snapshots — 月度在籍人數快照（L2）

結算用班級/全校在籍人數快照：HR 檢視/手調/確認後薪資引擎讀快照，
無快照月份 fallback 即時計算。classroom_id NULL = 全校總數列。

PG unique 視 NULL 各自相異 → 全校列唯一性用 partial unique index 補強。

Refs: docs/superpowers/specs/2026-06-13-enrollment-count-correctness-design.md
Revision ID: enrsnap01
Revises: dbck01
"""

import sqlalchemy as sa
from alembic import op

revision = "enrsnap01"
down_revision = "dbck01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "class_enrollment_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("snapshot_year", sa.Integer(), nullable=False),
        sa.Column("snapshot_month", sa.Integer(), nullable=False),
        sa.Column("classroom_id", sa.Integer(), nullable=True),
        sa.Column("student_count", sa.Numeric(6, 1), nullable=False),
        sa.Column(
            "count_mode",
            sa.String(length=20),
            nullable=False,
            server_default="month_end",
        ),
        sa.Column(
            "is_confirmed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("confirmed_by", sa.String(length=50), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("adjust_reason", sa.String(length=200), nullable=True),
        sa.Column("updated_by", sa.String(length=50), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"]),
    )
    op.create_index(
        "ix_enrollment_snapshot_ym",
        "class_enrollment_snapshots",
        ["snapshot_year", "snapshot_month"],
    )
    op.create_index(
        "ux_enrollment_snapshot_ym_class",
        "class_enrollment_snapshots",
        ["snapshot_year", "snapshot_month", "classroom_id"],
        unique=True,
        postgresql_where=sa.text("classroom_id IS NOT NULL"),
    )
    op.create_index(
        "ux_enrollment_snapshot_ym_school",
        "class_enrollment_snapshots",
        ["snapshot_year", "snapshot_month"],
        unique=True,
        postgresql_where=sa.text("classroom_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_enrollment_snapshot_ym_school", table_name="class_enrollment_snapshots"
    )
    op.drop_index(
        "ux_enrollment_snapshot_ym_class", table_name="class_enrollment_snapshots"
    )
    op.drop_index("ix_enrollment_snapshot_ym", table_name="class_enrollment_snapshots")
    op.drop_table("class_enrollment_snapshots")
