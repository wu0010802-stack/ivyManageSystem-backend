"""user_line_push_consent: 家長 LINE 推播跨境同意 flag

Revision ID: lncon01
Revises: intghealth01
Create Date: 2026-05-28
"""
import sqlalchemy as sa
from alembic import op

revision = "lncon01"
down_revision = "intghealth01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "line_push_consent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="LINE 推播跨境傳輸同意（P0 #6 / Spec E）；opt-in 預設 False",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "line_push_consent")
