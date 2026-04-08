"""add paid_amount to activity_registrations and create activity_payment_records

Revision ID: x3y4z5a6b7c8
Revises: w2x3y4z5a6b7
Create Date: 2026-03-30 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "x3y4z5a6b7c8"
down_revision = "w2x3y4z5a6b7"
branch_labels = None
depends_on = None


def _existing_columns(bind, table: str) -> set[str]:
    return {c["name"] for c in inspect(bind).get_columns(table)}


def _existing_tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    # 1. 在 activity_registrations 加入 paid_amount 欄位
    if "activity_registrations" in tables:
        existing_cols = _existing_columns(bind, "activity_registrations")
        if "paid_amount" not in existing_cols:
            op.add_column(
                "activity_registrations",
                sa.Column("paid_amount", sa.Integer(), nullable=False, server_default="0"),
            )

    # 2. 建立 activity_payment_records 表
    if "activity_payment_records" not in tables:
        op.create_table(
            "activity_payment_records",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("registration_id", sa.Integer(), nullable=False),
            sa.Column("type", sa.String(length=10), nullable=False, server_default="payment"),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("payment_date", sa.Date(), nullable=False),
            sa.Column("payment_method", sa.String(length=20), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("operator", sa.String(length=50), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["registration_id"],
                ["activity_registrations.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_activity_payment_records_reg",
            "activity_payment_records",
            ["registration_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if "activity_payment_records" in tables:
        op.drop_index("ix_activity_payment_records_reg", table_name="activity_payment_records")
        op.drop_table("activity_payment_records")

    if "activity_registrations" in tables:
        existing_cols = _existing_columns(bind, "activity_registrations")
        if "paid_amount" in existing_cols:
            op.drop_column("activity_registrations", "paid_amount")
