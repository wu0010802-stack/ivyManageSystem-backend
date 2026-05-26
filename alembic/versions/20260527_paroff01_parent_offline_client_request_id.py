"""parent offline client_request_id 2 tables (replies + leaves)

Revision ID: paroff01
Revises: mergeheads05
Create Date: 2026-05-27
"""

from alembic import op
import sqlalchemy as sa

revision = "paroff01"
down_revision = "mergeheads05"
branch_labels = None
depends_on = None

TABLES = ("student_contact_book_replies", "student_leave_requests")


def upgrade():
    for tbl in TABLES:
        op.add_column(
            tbl,
            sa.Column("client_request_id", sa.String(length=64), nullable=True),
        )
        op.create_index(
            f"ix_{tbl}_client_request_id",
            tbl,
            ["client_request_id"],
            unique=True,
            postgresql_where=sa.text("client_request_id IS NOT NULL"),
        )


def downgrade():
    for tbl in TABLES:
        op.drop_index(f"ix_{tbl}_client_request_id", table_name=tbl)
        op.drop_column(tbl, "client_request_id")
