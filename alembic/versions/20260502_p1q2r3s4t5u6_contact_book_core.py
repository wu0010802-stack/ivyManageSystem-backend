"""家長入口 v3.1 — Phase 1：每日聯絡簿核心三表

Revision ID: p1q2r3s4t5u6
Revises: 9e4549832715
Create Date: 2026-05-02

新增 student_contact_book_entries / _acks / _replies。
照片附件沿用既有 attachments 多型表（owner_type='contact_book_entry'）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "p1q2r3s4t5u6"
down_revision: Union[str, Sequence[str], None] = "9e4549832715"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "student_contact_book_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "student_id",
            sa.Integer(),
            sa.ForeignKey("students.id"),
            nullable=False,
        ),
        sa.Column(
            "classroom_id",
            sa.Integer(),
            sa.ForeignKey("classrooms.id"),
            nullable=False,
        ),
        sa.Column("log_date", sa.Date(), nullable=False),
        sa.Column("mood", sa.String(20), nullable=True),
        sa.Column("meal_lunch", sa.SmallInteger(), nullable=True),
        sa.Column("meal_snack", sa.SmallInteger(), nullable=True),
        sa.Column("nap_minutes", sa.SmallInteger(), nullable=True),
        sa.Column("bowel", sa.String(20), nullable=True),
        sa.Column("temperature_c", sa.Numeric(4, 1), nullable=True),
        sa.Column("teacher_note", sa.Text(), nullable=True),
        sa.Column("learning_highlight", sa.Text(), nullable=True),
        sa.Column(
            "created_by_employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id"),
            nullable=True,
        ),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "student_id", "log_date", name="uq_contact_book_student_date"
        ),
    )
    op.create_index(
        "ix_contact_book_classroom_date",
        "student_contact_book_entries",
        ["classroom_id", "log_date"],
    )
    op.create_index(
        "ix_contact_book_published",
        "student_contact_book_entries",
        ["published_at"],
    )
    op.create_index(
        "ix_contact_book_deleted",
        "student_contact_book_entries",
        ["deleted_at"],
    )

    op.create_table(
        "student_contact_book_acks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "entry_id",
            sa.Integer(),
            sa.ForeignKey("student_contact_book_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "guardian_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "read_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "entry_id",
            "guardian_user_id",
            name="uq_contact_book_ack_entry_guardian",
        ),
    )
    op.create_index(
        "ix_contact_book_ack_entry",
        "student_contact_book_acks",
        ["entry_id"],
    )

    op.create_table(
        "student_contact_book_replies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "entry_id",
            sa.Integer(),
            sa.ForeignKey("student_contact_book_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "guardian_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_contact_book_reply_entry_created",
        "student_contact_book_replies",
        ["entry_id", "created_at"],
    )
    op.create_index(
        "ix_contact_book_reply_deleted",
        "student_contact_book_replies",
        ["deleted_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_contact_book_reply_deleted", table_name="student_contact_book_replies"
    )
    op.drop_index(
        "ix_contact_book_reply_entry_created",
        table_name="student_contact_book_replies",
    )
    op.drop_table("student_contact_book_replies")

    op.drop_index("ix_contact_book_ack_entry", table_name="student_contact_book_acks")
    op.drop_table("student_contact_book_acks")

    op.drop_index("ix_contact_book_deleted", table_name="student_contact_book_entries")
    op.drop_index(
        "ix_contact_book_published", table_name="student_contact_book_entries"
    )
    op.drop_index(
        "ix_contact_book_classroom_date",
        table_name="student_contact_book_entries",
    )
    op.drop_table("student_contact_book_entries")
