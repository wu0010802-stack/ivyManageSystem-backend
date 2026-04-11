"""add recruitment sync states

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
Create Date: 2026-04-12 01:30:00.000000
"""

from alembic import op
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, inspect


revision = "m3n4o5p6q7r8"
down_revision = "l2m3n4o5p6q7"
branch_labels = None
depends_on = None


TABLE = "recruitment_sync_states"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    if TABLE not in inspector.get_table_names():
        op.create_table(
            TABLE,
            Column("id", Integer, primary_key=True),
            Column("provider_name", String(50), nullable=False),
            Column("provider_label", String(100), nullable=True),
            Column("sync_in_progress", Boolean, nullable=False, server_default="0"),
            Column("last_started_at", DateTime, nullable=True),
            Column("last_synced_at", DateTime, nullable=True),
            Column("last_sync_status", String(20), nullable=True),
            Column("last_sync_message", Text, nullable=True),
            Column("last_sync_counts", Text, nullable=True),
            Column("created_at", DateTime, nullable=True),
            Column("updated_at", DateTime, nullable=True),
        )
        op.create_index("ix_recruitment_sync_states_provider_name", TABLE, ["provider_name"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if TABLE not in inspector.get_table_names():
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes(TABLE)}
    if "ix_recruitment_sync_states_provider_name" in existing_indexes:
        op.drop_index("ix_recruitment_sync_states_provider_name", table_name=TABLE)
    op.drop_table(TABLE)
