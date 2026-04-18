"""add activity_pos_daily_close table

新增才藝課 POS 日結簽核表：老闆每日核對 POS 流水後簽核，凍結當日 snapshot
（payment_total / refund_total / by_method），避免事後補收/改帳讓對帳失效。

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-04-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "b0c1d2e3f4a5"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()
    if "activity_pos_daily_close" in tables:
        return

    op.create_table(
        "activity_pos_daily_close",
        sa.Column("close_date", sa.Date(), primary_key=True, nullable=False),
        sa.Column("approver_username", sa.String(length=50), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("payment_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("refund_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("net_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "transaction_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "by_method_json", sa.Text(), nullable=False, server_default="{}"
        ),
        sa.Column("actual_cash_count", sa.Integer(), nullable=True),
        sa.Column("cash_variance", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_activity_pos_daily_close_approver",
        "activity_pos_daily_close",
        ["approver_username"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "activity_pos_daily_close" not in inspect(bind).get_table_names():
        return
    op.drop_index(
        "ix_activity_pos_daily_close_approver",
        table_name="activity_pos_daily_close",
    )
    op.drop_table("activity_pos_daily_close")
