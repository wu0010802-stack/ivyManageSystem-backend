"""Add recurrence_rule JSONB column to school_events

Revision ID: recurr01
Revises: fkidx001
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "recurr01"
down_revision = "fkidx001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "school_events",
        sa.Column(
            "recurrence_rule",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="重複規則 JSONB；null 表單次事件",
        ),
    )


def downgrade():
    op.drop_column("school_events", "recurrence_rule")
