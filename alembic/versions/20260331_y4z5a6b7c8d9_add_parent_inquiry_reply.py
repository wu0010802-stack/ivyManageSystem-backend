"""add reply fields to parent_inquiries

Revision ID: y4z5a6b7c8d9
Revises: x3y4z5a6b7c8
Create Date: 2026-03-31
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "y4z5a6b7c8d9"
down_revision = "x3y4z5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns("parent_inquiries")}
    if "reply" not in cols:
        op.add_column("parent_inquiries", sa.Column("reply", sa.Text(), nullable=True))
    if "replied_at" not in cols:
        op.add_column("parent_inquiries", sa.Column("replied_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in inspect(bind).get_columns("parent_inquiries")}
    if "replied_at" in cols:
        op.drop_column("parent_inquiries", "replied_at")
    if "reply" in cols:
        op.drop_column("parent_inquiries", "reply")
