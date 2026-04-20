"""create parent_communication_logs table

學生紀錄抽屜 Phase B：新增「家長溝通紀錄」表，記錄與家長的電話、LINE、
面談、Email 等溝通內容，供教保員查詢追蹤。

Revision ID: g5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-04-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "g5b6c7d8e9f0"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = inspector.get_table_names()
    if "parent_communication_logs" in tables:
        return
    if "students" not in tables:
        return

    op.create_table(
        "parent_communication_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "student_id",
            sa.Integer,
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("communication_date", sa.Date, nullable=False),
        sa.Column("communication_type", sa.String(length=20), nullable=False),
        sa.Column("topic", sa.String(length=100), nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("follow_up", sa.Text, nullable=True),
        sa.Column(
            "recorded_by",
            sa.Integer,
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_parent_comm_student", "parent_communication_logs", ["student_id"]
    )
    op.create_index(
        "ix_parent_comm_date", "parent_communication_logs", ["communication_date"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "parent_communication_logs" not in inspect(bind).get_table_names():
        return
    existing = {
        ix["name"] for ix in inspect(bind).get_indexes("parent_communication_logs")
    }
    for name in ("ix_parent_comm_date", "ix_parent_comm_student"):
        if name in existing:
            op.drop_index(name, table_name="parent_communication_logs")
    op.drop_table("parent_communication_logs")
