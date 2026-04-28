"""家長入口 2.0 — Phase 3：parent_message_threads / parent_messages / line_webhook_events

Revision ID: l7i8j9k0l1m2
Revises: k6h7i8j9k0l1
Create Date: 2026-04-29
"""

import sqlalchemy as sa
from alembic import op

revision = "l7i8j9k0l1m2"
down_revision = "k6h7i8j9k0l1"
branch_labels = None
depends_on = None


def upgrade():
    # parent_message_threads
    op.create_table(
        "parent_message_threads",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "parent_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "teacher_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "student_id",
            sa.Integer(),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("parent_last_read_at", sa.DateTime(), nullable=True),
        sa.Column("teacher_last_read_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "parent_user_id",
            "teacher_user_id",
            "student_id",
            name="uq_parent_thread_triple",
        ),
    )
    op.create_index(
        "ix_parent_thread_parent_lastmsg",
        "parent_message_threads",
        ["parent_user_id", "last_message_at"],
    )
    op.create_index(
        "ix_parent_thread_teacher_lastmsg",
        "parent_message_threads",
        ["teacher_user_id", "last_message_at"],
    )
    op.create_index(
        "ix_parent_thread_student",
        "parent_message_threads",
        ["student_id"],
    )

    # parent_messages
    op.create_table(
        "parent_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "thread_id",
            sa.Integer(),
            sa.ForeignKey("parent_message_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sender_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sender_role",
            sa.String(length=10),
            nullable=False,
            comment="'parent' 或 'teacher'",
        ),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("client_request_id", sa.String(length=64), nullable=True),
        sa.Column(
            "source",
            sa.String(length=10),
            nullable=False,
            server_default="app",
        ),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "thread_id",
            "client_request_id",
            name="uq_parent_msg_client_request",
        ),
    )
    op.create_index(
        "ix_parent_msg_thread_created",
        "parent_messages",
        ["thread_id", "created_at"],
    )
    op.create_index(
        "ix_parent_msg_sender",
        "parent_messages",
        ["sender_user_id"],
    )

    # line_webhook_events
    op.create_table(
        "line_webhook_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("webhook_event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("line_user_id", sa.String(length=100), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("webhook_event_id", name="uq_line_webhook_event_id"),
    )
    op.create_index(
        "ix_line_webhook_user_created",
        "line_webhook_events",
        ["line_user_id", "created_at"],
    )
    op.create_index(
        "ix_line_webhook_created",
        "line_webhook_events",
        ["created_at"],
    )


def downgrade():
    op.drop_index("ix_line_webhook_created", table_name="line_webhook_events")
    op.drop_index("ix_line_webhook_user_created", table_name="line_webhook_events")
    op.drop_table("line_webhook_events")

    op.drop_index("ix_parent_msg_sender", table_name="parent_messages")
    op.drop_index("ix_parent_msg_thread_created", table_name="parent_messages")
    op.drop_table("parent_messages")

    op.drop_index("ix_parent_thread_student", table_name="parent_message_threads")
    op.drop_index(
        "ix_parent_thread_teacher_lastmsg", table_name="parent_message_threads"
    )
    op.drop_index(
        "ix_parent_thread_parent_lastmsg", table_name="parent_message_threads"
    )
    op.drop_table("parent_message_threads")
