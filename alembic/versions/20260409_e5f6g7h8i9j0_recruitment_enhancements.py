"""recruitment model enhancements

新增 recruitment_visits 延伸欄位（未預繳原因、已註冊、轉期）
新增 recruitment_periods 近五年期間轉換整合表

Revision ID: e5f6g7h8i9j0
Revises: d4e5f6g7h8i9
Create Date: 2026-04-09 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = "e5f6g7h8i9j0"
down_revision = "d4e5f6g7h8i9"
branch_labels = None
depends_on = None


def _col_exists(inspector, table, col):
    return col in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = inspector.get_table_names()

    # --- 新增 recruitment_visits 欄位 ---
    if "recruitment_visits" in tables:
        for col, col_def in [
            ("no_deposit_reason", "VARCHAR(60)"),
            ("no_deposit_reason_detail", "TEXT"),
            ("enrolled", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("transfer_term", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ]:
            if not _col_exists(inspector, "recruitment_visits", col):
                bind.execute(text(
                    f"ALTER TABLE recruitment_visits ADD COLUMN {col} {col_def}"
                ))

    # --- 建立 recruitment_periods 表 ---
    if "recruitment_periods" not in tables:
        op.create_table(
            "recruitment_periods",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("period_name", sa.String(length=50), nullable=False),
            sa.Column("visit_count", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("deposit_count", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("enrolled_count", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("transfer_term_count", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("effective_deposit_count", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("not_enrolled_deposit", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("enrolled_after_school", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=True, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("period_name"),
        )
        op.create_index("ix_recruitment_periods_id", "recruitment_periods", ["id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = inspector.get_table_names()

    if "recruitment_periods" in tables:
        op.drop_index("ix_recruitment_periods_id", table_name="recruitment_periods")
        op.drop_table("recruitment_periods")

    if "recruitment_visits" in tables:
        for col in ["no_deposit_reason", "no_deposit_reason_detail", "enrolled", "transfer_term"]:
            if _col_exists(inspector, "recruitment_visits", col):
                bind.execute(text(f"ALTER TABLE recruitment_visits DROP COLUMN {col}"))
