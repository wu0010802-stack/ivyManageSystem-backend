"""parent_rls_phase1d_milestones: student_milestones RLS

Revision ID: parlsr005
Revises: parlsr004
Create Date: 2026-05-18

Phase 1d (narrowed) — student_milestones only.

The original Phase 1d scope listed milestones + contact_book + medications +
activity. The latter three all need new owner_type ELSIF branches on
`parent_owns_attachment`, so they're consolidated into Phase 1e (single
migration touching the function + three table policies). milestones is
attachment-free and fits as a clean standalone batch.

# Table
`student_milestones` — direct `student_id`. Parent flows:
- GET list (SELECT)
- POST react → UPDATE parent_reaction + parent_acknowledged_at +
  parent_acknowledged_by (Guardian.id)
- POST acknowledge → UPDATE parent_acknowledged_at + parent_acknowledged_by

Both writes use `with_for_update` row-lock (defends against two guardians of
the same student double-acking). The lock works under RLS — the FOR UPDATE
applies to rows that pass USING, which is exactly the set we want.

# Policy: FOR ALL
Same shape as student_growth_reports in parlsr004 — needs both SELECT and
UPDATE permission. WITH CHECK mirrors USING; parent can't change `student_id`
to escape isolation because the WITH CHECK would reject the post-state.
"""

from __future__ import annotations

from alembic import op

revision = "parlsr005"
down_revision = "parlsr004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        GRANT SELECT, UPDATE ON student_milestones TO ivy_parent_role;

        ALTER TABLE student_milestones ENABLE ROW LEVEL SECURITY;
        ALTER TABLE student_milestones FORCE  ROW LEVEL SECURITY;

        CREATE POLICY parent_isolate_student_milestones ON student_milestones
        FOR ALL TO ivy_parent_role
        USING (
            student_id IN (
                SELECT g.student_id FROM guardians g
                WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                  AND g.deleted_at IS NULL
            )
        )
        WITH CHECK (
            student_id IN (
                SELECT g.student_id FROM guardians g
                WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                  AND g.deleted_at IS NULL
            )
        );
        """)


def downgrade() -> None:
    op.execute("""
        DROP POLICY IF EXISTS parent_isolate_student_milestones ON student_milestones;

        ALTER TABLE student_milestones NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE student_milestones DISABLE  ROW LEVEL SECURITY;

        REVOKE SELECT, UPDATE ON student_milestones FROM ivy_parent_role;
        """)
