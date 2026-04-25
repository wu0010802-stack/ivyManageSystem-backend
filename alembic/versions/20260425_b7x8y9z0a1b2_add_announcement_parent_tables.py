"""add announcement_parent_recipients + announcement_parent_reads

家長入口 Batch 4：員工端 announcement_recipients 不動，另建家長端兩張
表。可見性規則由 SQL OR 計算（見 api/parent_portal/announcements.py）。

Revision ID: b7x8y9z0a1b2
Revises: a6w7x8y9z0a1
Create Date: 2026-04-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "b7x8y9z0a1b2"
down_revision = "a6w7x8y9z0a1"
branch_labels = None
depends_on = None


def _index_names(bind, table: str) -> set:
    if table not in inspect(bind).get_table_names():
        return set()
    return {ix["name"] for ix in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()

    if "announcement_parent_recipients" not in tables and {"announcements", "classrooms", "students", "guardians"}.issubset(tables):
        op.create_table(
            "announcement_parent_recipients",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "announcement_id",
                sa.Integer,
                sa.ForeignKey("announcements.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("scope", sa.String(length=20), nullable=False),
            sa.Column(
                "classroom_id",
                sa.Integer,
                sa.ForeignKey("classrooms.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column(
                "student_id",
                sa.Integer,
                sa.ForeignKey("students.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column(
                "guardian_id",
                sa.Integer,
                sa.ForeignKey("guardians.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_ann_parent_scope",
            "announcement_parent_recipients",
            ["announcement_id", "scope"],
        )
        op.create_index(
            "ix_ann_parent_classroom",
            "announcement_parent_recipients",
            ["classroom_id"],
        )
        op.create_index(
            "ix_ann_parent_student",
            "announcement_parent_recipients",
            ["student_id"],
        )

    if "announcement_parent_reads" not in tables and {"announcements", "users"}.issubset(tables):
        op.create_table(
            "announcement_parent_reads",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column(
                "announcement_id",
                sa.Integer,
                sa.ForeignKey("announcements.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                sa.Integer,
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "read_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "announcement_id", "user_id", name="uq_ann_parent_read"
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = inspect(bind).get_table_names()

    if "announcement_parent_reads" in tables:
        op.drop_table("announcement_parent_reads")
    if "announcement_parent_recipients" in tables:
        for ix in (
            "ix_ann_parent_student",
            "ix_ann_parent_classroom",
            "ix_ann_parent_scope",
        ):
            if ix in _index_names(bind, "announcement_parent_recipients"):
                op.drop_index(ix, table_name="announcement_parent_recipients")
        op.drop_table("announcement_parent_recipients")
