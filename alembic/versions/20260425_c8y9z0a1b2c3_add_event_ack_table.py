"""add event_acknowledgments + school_events.requires_acknowledgment / ack_deadline

家長入口 Batch 4：事件簽閱機制。

Revision ID: c8y9z0a1b2c3
Revises: b7x8y9z0a1b2
Create Date: 2026-04-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "c8y9z0a1b2c3"
down_revision = "b7x8y9z0a1b2"
branch_labels = None
depends_on = None


def _column_names(bind, table: str) -> set:
    if table not in inspect(bind).get_table_names():
        return set()
    return {c["name"] for c in inspect(bind).get_columns(table)}


def _index_names(bind, table: str) -> set:
    if table not in inspect(bind).get_table_names():
        return set()
    return {ix["name"] for ix in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()

    # SchoolEvent 加兩個欄位
    if "school_events" in tables:
        cols = _column_names(bind, "school_events")
        if "requires_acknowledgment" not in cols:
            op.add_column(
                "school_events",
                sa.Column(
                    "requires_acknowledgment",
                    sa.Boolean,
                    nullable=False,
                    server_default=sa.text("false"),
                ),
            )
        if "ack_deadline" not in cols:
            op.add_column(
                "school_events",
                sa.Column("ack_deadline", sa.Date, nullable=True),
            )

    # event_acknowledgments 新表
    if "event_acknowledgments" not in tables and {"school_events", "users", "students"}.issubset(tables):
        op.create_table(
            "event_acknowledgments",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "event_id",
                sa.Integer,
                sa.ForeignKey("school_events.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                sa.Integer,
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "student_id",
                sa.Integer,
                sa.ForeignKey("students.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "acknowledged_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("signature_name", sa.String(length=50), nullable=True),
            sa.UniqueConstraint(
                "event_id", "user_id", "student_id", name="uq_event_ack"
            ),
        )
        op.create_index("ix_event_ack_event", "event_acknowledgments", ["event_id"])
        op.create_index("ix_event_ack_user", "event_acknowledgments", ["user_id"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()

    if "event_acknowledgments" in tables:
        for ix in ("ix_event_ack_user", "ix_event_ack_event"):
            if ix in _index_names(bind, "event_acknowledgments"):
                op.drop_index(ix, table_name="event_acknowledgments")
        op.drop_table("event_acknowledgments")

    if "school_events" in tables:
        cols = _column_names(bind, "school_events")
        if "ack_deadline" in cols:
            op.drop_column("school_events", "ack_deadline")
        if "requires_acknowledgment" in cols:
            op.drop_column("school_events", "requires_acknowledgment")
