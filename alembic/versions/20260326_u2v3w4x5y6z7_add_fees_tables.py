"""add fee_items and student_fee_records tables

Revision ID: u2v3w4x5y6z7
Revises: t1u2v3w4x5y6
Create Date: 2026-03-26 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "u2v3w4x5y6z7"
down_revision = "t1u2v3w4x5y6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "fee_items" not in tables:
        op.create_table(
            "fee_items",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("amount", sa.Integer(), nullable=False),
            sa.Column("classroom_id", sa.Integer(), nullable=True),
            sa.Column("period", sa.String(length=20), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["classroom_id"], ["classrooms.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_fee_items_period_active", "fee_items", ["period", "is_active"])
        op.create_index("ix_fee_items_classroom", "fee_items", ["classroom_id"])

    if "student_fee_records" not in tables:
        op.create_table(
            "student_fee_records",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("student_id", sa.Integer(), nullable=False),
            sa.Column("student_name", sa.String(length=50), nullable=False),
            sa.Column("classroom_name", sa.String(length=50), nullable=True),
            sa.Column("fee_item_id", sa.Integer(), nullable=False),
            sa.Column("fee_item_name", sa.String(length=100), nullable=False),
            sa.Column("amount_due", sa.Integer(), nullable=False),
            sa.Column("amount_paid", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=10), nullable=False),
            sa.Column("payment_date", sa.Date(), nullable=True),
            sa.Column("payment_method", sa.String(length=20), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("period", sa.String(length=20), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["fee_item_id"], ["fee_items.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint("student_id", "fee_item_id", name="uq_student_fee_item"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_fee_records_period_status", "student_fee_records", ["period", "status"])
        op.create_index("ix_fee_records_student", "student_fee_records", ["student_id"])
        op.create_index("ix_fee_records_fee_item", "student_fee_records", ["fee_item_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "student_fee_records" in tables:
        op.drop_index("ix_fee_records_fee_item", table_name="student_fee_records")
        op.drop_index("ix_fee_records_student", table_name="student_fee_records")
        op.drop_index("ix_fee_records_period_status", table_name="student_fee_records")
        op.drop_table("student_fee_records")

    if "fee_items" in tables:
        op.drop_index("ix_fee_items_classroom", table_name="fee_items")
        op.drop_index("ix_fee_items_period_active", table_name="fee_items")
        op.drop_table("fee_items")
