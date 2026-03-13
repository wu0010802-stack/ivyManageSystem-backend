"""add report_snapshots cache table

Revision ID: 8f1b2c3d4e5f
Revises: c6a7b9d1e2f3
Create Date: 2026-03-13 15:50:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision = "8f1b2c3d4e5f"
down_revision = "c6a7b9d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "report_snapshots" in inspector.get_table_names():
        return

    op.create_table(
        "report_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("cache_key", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cache_key"),
    )
    op.create_index("ix_report_snapshots_category", "report_snapshots", ["category"], unique=False)
    op.create_index("ix_report_snapshots_expires_at", "report_snapshots", ["expires_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "report_snapshots" not in inspector.get_table_names():
        return

    op.drop_index("ix_report_snapshots_expires_at", table_name="report_snapshots")
    op.drop_index("ix_report_snapshots_category", table_name="report_snapshots")
    op.drop_table("report_snapshots")
