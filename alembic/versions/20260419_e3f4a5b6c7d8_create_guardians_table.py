"""create guardians table and backfill from students.parent/emergency fields

學生紀錄追蹤大功能 Phase A：
- 建立 `guardians` 表（一位學生對應多位監護人）
- 從現有 `students.parent_name/parent_phone` 回填一筆 `is_primary=True`
- 從現有 `students.emergency_contact_*` 回填一筆 `is_emergency=True, can_pickup=True`

`students.parent_name/phone` 保留為快照欄位（相容期），不在此 migration 移除。

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-04-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "e3f4a5b6c7d8"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def _existing_indexes(bind, table: str) -> set:
    return {ix["name"] for ix in inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = inspector.get_table_names()
    if "guardians" in tables:
        return
    if "students" not in tables:
        return

    op.create_table(
        "guardians",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "student_id",
            sa.Integer,
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("email", sa.String(length=100), nullable=True),
        sa.Column("relation", sa.String(length=20), nullable=True),
        sa.Column(
            "is_primary",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "is_emergency",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "can_pickup",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("custody_note", sa.Text, nullable=True),
        sa.Column(
            "sort_order", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.Column(
            "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_guardians_student", "guardians", ["student_id"])
    op.create_index(
        "ix_guardians_student_active", "guardians", ["student_id", "deleted_at"]
    )
    op.create_index("ix_guardians_phone", "guardians", ["phone"])

    # --- Backfill from students.parent_* ---
    bind.execute(
        sa.text(
            """
            INSERT INTO guardians
                (student_id, name, phone, relation, is_primary, is_emergency,
                 can_pickup, sort_order, created_at, updated_at)
            SELECT
                id,
                COALESCE(NULLIF(TRIM(parent_name), ''), '家長'),
                NULLIF(TRIM(parent_phone), ''),
                '監護人',
                TRUE, FALSE, FALSE, 0, NOW(), NOW()
            FROM students
            WHERE COALESCE(NULLIF(TRIM(parent_name), ''), NULLIF(TRIM(parent_phone), '')) IS NOT NULL
            """
        )
    )

    # --- Backfill from students.emergency_contact_* ---
    bind.execute(
        sa.text(
            """
            INSERT INTO guardians
                (student_id, name, phone, relation, is_primary, is_emergency,
                 can_pickup, sort_order, created_at, updated_at)
            SELECT
                id,
                COALESCE(NULLIF(TRIM(emergency_contact_name), ''), '緊急聯絡人'),
                NULLIF(TRIM(emergency_contact_phone), ''),
                COALESCE(NULLIF(TRIM(emergency_contact_relation), ''), '緊急聯絡人'),
                FALSE, TRUE, TRUE, 1, NOW(), NOW()
            FROM students
            WHERE COALESCE(NULLIF(TRIM(emergency_contact_name), ''),
                           NULLIF(TRIM(emergency_contact_phone), '')) IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "guardians" not in inspect(bind).get_table_names():
        return
    idx = _existing_indexes(bind, "guardians")
    for name in (
        "ix_guardians_phone",
        "ix_guardians_student_active",
        "ix_guardians_student",
    ):
        if name in idx:
            op.drop_index(name, table_name="guardians")
    op.drop_table("guardians")
