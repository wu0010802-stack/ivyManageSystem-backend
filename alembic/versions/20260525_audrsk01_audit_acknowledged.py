"""audit_logs add acknowledged_at / acknowledged_by

Revision ID: audrsk01
Revises: rolesdb01
Create Date: 2026-05-25

紅點機制：高風險 audit 事件需要 ack（標已讀）。
新增 2 nullable 欄位 + FK + index。Postgres 11+ nullable column add 為
metadata-only operation，無鎖風險。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "audrsk01"
down_revision: Union[str, None] = "rolesdb01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "audit_logs",
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("acknowledged_by", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_audit_logs_acknowledged_by",
        "audit_logs",
        "users",
        ["acknowledged_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_audit_logs_ack_created",
        "audit_logs",
        ["acknowledged_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_ack_created", table_name="audit_logs")
    op.drop_constraint(
        "fk_audit_logs_acknowledged_by", "audit_logs", type_="foreignkey"
    )
    op.drop_column("audit_logs", "acknowledged_by")
    op.drop_column("audit_logs", "acknowledged_at")
