"""vendor_payments: 廠商付款簽收（園務行政）

Revision ID: vndpay01
Revises: aprsig001
Create Date: 2026-05-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "vndpay01"
down_revision = "aprsig001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vendor_payments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("payment_date", sa.Date, nullable=False),
        sa.Column("vendor_name", sa.String(120), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("payment_method", sa.String(20), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("invoice_number", sa.String(60), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "attachments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "signer_id",
            sa.Integer,
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("signed_at", sa.DateTime, nullable=True),
        sa.Column("signature_kind", sa.String(16), nullable=True),
        sa.Column("signature_key", sa.String(255), nullable=True),
        sa.Column(
            "created_by_id",
            sa.Integer,
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("amount >= 0", name="ck_vendor_payments_amount_nonneg"),
        sa.CheckConstraint(
            "payment_method IN ('cash','bank_transfer','check','linepay','other')",
            name="ck_vendor_payments_method",
        ),
        sa.CheckConstraint(
            "status IN ('pending','signed')",
            name="ck_vendor_payments_status",
        ),
        sa.CheckConstraint(
            "signature_kind IS NULL OR signature_kind IN ('drawn','photo')",
            name="ck_vendor_payments_signature_kind",
        ),
    )
    op.create_index(
        "ix_vendor_payments_payment_date",
        "vendor_payments",
        ["payment_date"],
    )
    op.create_index(
        "ix_vendor_payments_vendor_name",
        "vendor_payments",
        ["vendor_name"],
    )
    op.create_index(
        "ix_vendor_payments_signer_id",
        "vendor_payments",
        ["signer_id"],
    )
    op.create_index(
        "ix_vendor_payments_status_date",
        "vendor_payments",
        ["status", "payment_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_vendor_payments_status_date", table_name="vendor_payments")
    op.drop_index("ix_vendor_payments_signer_id", table_name="vendor_payments")
    op.drop_index("ix_vendor_payments_vendor_name", table_name="vendor_payments")
    op.drop_index("ix_vendor_payments_payment_date", table_name="vendor_payments")
    op.drop_table("vendor_payments")
