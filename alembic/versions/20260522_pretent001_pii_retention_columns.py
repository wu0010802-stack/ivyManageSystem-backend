"""pretent001 — 加 students.terminal_entered_at + guardians.pii_redacted_at + backfill

Revision ID: pretent001
Revises: 3be2e40aaa42
Create Date: 2026-05-22
"""

from alembic import op
import sqlalchemy as sa

revision = "pretent001"
down_revision = "3be2e40aaa42"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "students",
        sa.Column(
            "terminal_entered_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "進入終態（graduated/transferred/withdrawn）的 UTC 時間戳；"
                "復學回 active 時 NULL；PII retention GC 計算用"
            ),
        ),
    )
    op.add_column(
        "guardians",
        sa.Column(
            "pii_redacted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Guardian PII 被 retention GC 抹除的時間戳；"
                "NOT NULL 即已抹過避免重複 GC"
            ),
        ),
    )

    op.create_index(
        "ix_student_terminal_retention",
        "students",
        ["terminal_entered_at", "lifecycle_status"],
        postgresql_where=sa.text("terminal_entered_at IS NOT NULL"),
    )
    op.create_index(
        "ix_guardians_pii_redacted_null",
        "guardians",
        ["student_id"],
        postgresql_where=sa.text("pii_redacted_at IS NULL"),
    )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("""
            WITH lifecycle_changes AS (
                SELECT
                    CAST(entity_id AS INTEGER) AS student_id,
                    MAX(created_at) AS last_change_at
                FROM audit_logs
                WHERE entity_type = 'student'
                  AND action IN ('UPDATE', 'CREATE')
                  AND (changes LIKE '%lifecycle_status%' OR summary LIKE '%lifecycle%')
                  AND entity_id ~ '^\\d+$'
                -- NOTE: audit_logs.entity_id 是 String(50)，須先用 regex 過濾純數字再 CAST
                GROUP BY CAST(entity_id AS INTEGER)
            )
            UPDATE students s
            SET terminal_entered_at = COALESCE(lc.last_change_at, s.updated_at)
            FROM lifecycle_changes lc
            WHERE s.id = lc.student_id
              AND s.lifecycle_status IN ('graduated', 'transferred', 'withdrawn')
              AND s.terminal_entered_at IS NULL;

            UPDATE students
            SET terminal_entered_at = updated_at
            WHERE lifecycle_status IN ('graduated', 'transferred', 'withdrawn')
              AND terminal_entered_at IS NULL;
        """)
    else:
        # SQLite test fallback：直接用 updated_at
        op.execute("""
            UPDATE students
            SET terminal_entered_at = updated_at
            WHERE lifecycle_status IN ('graduated', 'transferred', 'withdrawn')
              AND terminal_entered_at IS NULL;
        """)


def downgrade():
    op.drop_index("ix_guardians_pii_redacted_null", table_name="guardians")
    op.drop_index("ix_student_terminal_retention", table_name="students")
    op.drop_column("guardians", "pii_redacted_at")
    op.drop_column("students", "terminal_entered_at")
