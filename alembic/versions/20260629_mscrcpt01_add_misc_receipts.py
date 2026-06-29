"""misc_receipts: 雜項收款簽收（園務行政，收入側）

Revision ID: mscrcpt01
Revises: parcuplk01
Create Date: 2026-06-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "mscrcpt01"
down_revision = "parcuplk01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "misc_receipts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("receipt_date", sa.Date, nullable=False),
        sa.Column("payer_name", sa.String(120), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("payment_method", sa.String(20), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("receipt_number", sa.String(60), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "attachments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
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
            "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("amount > 0", name="ck_misc_receipts_amount_pos"),
        sa.CheckConstraint(
            "payment_method IN ('cash','bank_transfer','check','linepay','other')",
            name="ck_misc_receipts_method",
        ),
        sa.CheckConstraint(
            "status IN ('pending','signed')", name="ck_misc_receipts_status"
        ),
        sa.CheckConstraint(
            "category IN ('rent','donation','subsidy','secondhand_sale','refund_recovery','other')",
            name="ck_misc_receipts_category",
        ),
        sa.CheckConstraint(
            "signature_kind IS NULL OR signature_kind IN ('drawn','photo')",
            name="ck_misc_receipts_signature_kind",
        ),
    )
    op.create_index("ix_misc_receipts_receipt_date", "misc_receipts", ["receipt_date"])
    op.create_index("ix_misc_receipts_payer_name", "misc_receipts", ["payer_name"])
    op.create_index("ix_misc_receipts_category", "misc_receipts", ["category"])
    op.create_index("ix_misc_receipts_signer_id", "misc_receipts", ["signer_id"])
    op.create_index(
        "ix_misc_receipts_status_date", "misc_receipts", ["status", "receipt_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_misc_receipts_status_date", table_name="misc_receipts")
    op.drop_index("ix_misc_receipts_signer_id", table_name="misc_receipts")
    op.drop_index("ix_misc_receipts_category", table_name="misc_receipts")
    op.drop_index("ix_misc_receipts_payer_name", table_name="misc_receipts")
    op.drop_index("ix_misc_receipts_receipt_date", table_name="misc_receipts")
    op.drop_table("misc_receipts")
