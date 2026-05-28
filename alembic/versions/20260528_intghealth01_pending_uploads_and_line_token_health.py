"""pending_uploads + line_token_health

Revision ID: intghealth01
Revises: notifretry01
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa

revision = "intghealth01"
down_revision = "notifretry01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pending_uploads",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("module", sa.String(40), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(80), nullable=False),
        sa.Column("local_path", sa.String(500), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("succeeded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_pending_uploads_next_retry",
        "pending_uploads",
        ["next_retry_at"],
        postgresql_where=sa.text("succeeded_at IS NULL AND attempts < 5"),
    )
    op.create_table(
        "line_token_health",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("healthy", sa.Boolean, nullable=False),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )


def downgrade():
    op.drop_table("line_token_health")
    op.drop_index("ix_pending_uploads_next_retry", table_name="pending_uploads")
    op.drop_table("pending_uploads")
