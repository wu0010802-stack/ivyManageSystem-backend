"""audit_logs add user_agent_hash + session_id

Revision ID: auditfor01
Revises: intghealth01
Create Date: 2026-05-29

Ch1 of observability-forensic-and-design-tokens spec.
新增兩欄供「家長帳號被盜」forensic：
- user_agent_hash: SHA256(UA)[:32]，hash 化避免直存 device PII
- session_id: JWT jti claim（stateless，無伺服端 session 表）
"""

from alembic import op
import sqlalchemy as sa

revision = "auditfor01"
down_revision = "intghealth01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_logs",
        sa.Column("user_agent_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("session_id", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_audit_session", "audit_logs", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_session", table_name="audit_logs")
    op.drop_column("audit_logs", "session_id")
    op.drop_column("audit_logs", "user_agent_hash")
