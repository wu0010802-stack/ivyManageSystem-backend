"""家長入口 2.0 — Phase 5：line_reply_contexts

Revision ID: n9k0l1m2n3o4
Revises: m8j9k0l1m2n3
Create Date: 2026-04-29
"""

import sqlalchemy as sa
from alembic import op

revision = "n9k0l1m2n3o4"
down_revision = "m8j9k0l1m2n3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "line_reply_contexts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("line_user_id", sa.String(length=100), nullable=False),
        sa.Column(
            "thread_id",
            sa.Integer(),
            sa.ForeignKey("parent_message_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("line_user_id", name="uq_line_reply_context_user"),
    )
    op.create_index(
        "ix_line_reply_context_expires",
        "line_reply_contexts",
        ["expires_at"],
    )


def downgrade():
    op.drop_index("ix_line_reply_context_expires", table_name="line_reply_contexts")
    op.drop_table("line_reply_contexts")
