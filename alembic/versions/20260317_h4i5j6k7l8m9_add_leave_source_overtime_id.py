"""add source_overtime_id to leave_records

補休假單與來源加班記錄建立關聯，
使 _revoke_comp_leave_grant 能精確識別哪些假單需自動駁回。

Revision ID: h4i5j6k7l8m9
Revises: g3h4i5j6k7l8
Create Date: 2026-03-17 00:03:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "h4i5j6k7l8m9"
down_revision = "g3h4i5j6k7l8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = [c["name"] for c in inspector.get_columns("leave_records")]
    if "source_overtime_id" not in cols:
        op.add_column(
            "leave_records",
            sa.Column(
                "source_overtime_id",
                sa.Integer(),
                sa.ForeignKey("overtime_records.id", ondelete="SET NULL"),
                nullable=True,
                comment="來源加班記錄 ID（補休假單專用）",
            ),
        )
        op.create_index(
            "ix_leave_source_overtime",
            "leave_records",
            ["source_overtime_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    indexes = {idx["name"] for idx in inspector.get_indexes("leave_records")}
    if "ix_leave_source_overtime" in indexes:
        op.drop_index("ix_leave_source_overtime", table_name="leave_records")

    cols = [c["name"] for c in inspector.get_columns("leave_records")]
    if "source_overtime_id" in cols:
        op.drop_column("leave_records", "source_overtime_id")
