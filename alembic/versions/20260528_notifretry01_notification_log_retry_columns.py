"""notification_logs add line retry columns + is_inbox_visible

Revision ID: notifretry01
Revises: mergeheads06
Create Date: 2026-05-28

對應 spec §6.2 Phase 2 P1 resilience。
- line_retry_count / line_next_retry_at：scheduler 撈 retry pending row
- is_inbox_visible：解開 inbox UX 與 retry audit 耦合（14 個 LINE-only 家長事件
  log row 也寫，但不出現在員工 inbox）
"""

from alembic import op
import sqlalchemy as sa

revision = "notifretry01"
down_revision = "mergeheads06"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "notification_logs",
        sa.Column("line_retry_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "notification_logs",
        sa.Column("line_next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "notification_logs",
        sa.Column(
            "is_inbox_visible",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.create_index(
        "ix_notif_log_line_retry_pending",
        "notification_logs",
        ["line_next_retry_at"],
        postgresql_where=sa.text(
            "line_next_retry_at IS NOT NULL AND line_retry_count < 3"
        ),
    )


def downgrade():
    op.drop_index("ix_notif_log_line_retry_pending", table_name="notification_logs")
    op.drop_column("notification_logs", "is_inbox_visible")
    op.drop_column("notification_logs", "line_next_retry_at")
    op.drop_column("notification_logs", "line_retry_count")
