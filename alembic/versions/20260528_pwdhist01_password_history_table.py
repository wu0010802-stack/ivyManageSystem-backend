"""password_history table for replay prevention.

Revision ID: pwdhist01
Revises: intghealth01
Create Date: 2026-05-28
"""

from alembic import op
import sqlalchemy as sa

revision = "pwdhist01"
down_revision = "intghealth01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_history",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_password_history_user_id_created_at",
        "password_history",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_password_history_user_id_created_at", table_name="password_history"
    )
    op.drop_table("password_history")
