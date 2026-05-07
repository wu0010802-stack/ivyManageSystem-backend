"""新增 activity_pos_daily_close_history 歷史表（spec H3）

H3 要求：unlock 不再是純 hard delete + ApprovalLog 文字摘要，而是 append-only
歷史快照，完整保存 by_method JSON 等結構化資料供日後稽核還原。

Why: 原 hard delete 將完整 snapshot 丟失，僅 ApprovalLog.comment 文字摘要保留
payment_total/refund_total/net_total，但 by_method 與盤點細節僅留 free text。
本 migration 建 append-only 表，每次 unlock 把原 snapshot 完整寫入。

Revision ID: f1g2h3i4j5k6
Revises: e0f1g2h3i4j5
Create Date: 2026-05-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "f1g2h3i4j5k6"
down_revision = "e0f1g2h3i4j5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "activity_pos_daily_close_history" in inspector.get_table_names():
        return  # idempotent

    op.create_table(
        "activity_pos_daily_close_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("close_date", sa.Date(), nullable=False),
        sa.Column("approver_username", sa.String(length=50), nullable=False),
        sa.Column("approver_role", sa.String(length=20), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.Column("approve_note", sa.Text(), nullable=True),
        sa.Column("payment_total", sa.Integer(), nullable=False),
        sa.Column("refund_total", sa.Integer(), nullable=False),
        sa.Column("net_total", sa.Integer(), nullable=False),
        sa.Column("transaction_count", sa.Integer(), nullable=False),
        sa.Column("by_method_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("actual_cash_count", sa.Integer(), nullable=True),
        sa.Column("cash_variance", sa.Integer(), nullable=True),
        sa.Column("unlocked_at", sa.DateTime(), nullable=False),
        sa.Column("unlocked_by", sa.String(length=50), nullable=False),
        sa.Column("unlocked_by_role", sa.String(length=20), nullable=True),
        sa.Column(
            "is_admin_override",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("unlock_reason", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_activity_pos_daily_close_history_close_date",
        "activity_pos_daily_close_history",
        ["close_date"],
    )
    op.create_index(
        "ix_activity_pos_daily_close_history_unlocked_at",
        "activity_pos_daily_close_history",
        ["unlocked_at"],
    )
    op.create_index(
        "ix_pos_close_history_date_unlocked",
        "activity_pos_daily_close_history",
        ["close_date", "unlocked_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "activity_pos_daily_close_history" not in inspector.get_table_names():
        return
    op.drop_index(
        "ix_pos_close_history_date_unlocked",
        table_name="activity_pos_daily_close_history",
    )
    op.drop_index(
        "ix_activity_pos_daily_close_history_unlocked_at",
        table_name="activity_pos_daily_close_history",
    )
    op.drop_index(
        "ix_activity_pos_daily_close_history_close_date",
        table_name="activity_pos_daily_close_history",
    )
    op.drop_table("activity_pos_daily_close_history")
