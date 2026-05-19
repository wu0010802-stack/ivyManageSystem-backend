"""parent_rls_phase1b_leaves: leaves + attachments (polymorphic) RLS

Revision ID: parlsr003
Revises: parlsr002
Create Date: 2026-05-18

Phase 1b — `leaves.py` 路由切到 parent_engine 的 DB 配套。

This migration finishes the Phase 1 pilot by adding:
1. write GRANTs on student_leave_requests + student_attendances (parents create
   leave requests, mutate them on cancel; cancel hard-deletes the auto-applied
   attendance rows via `revert_attendance_for_leave`)
2. polymorphic Attachment policy via a helper function — phase 1b ONLY
   recognises `owner_type='student_leave'`; other owner_types fail-closed.
   Future phases extend `parent_owns_attachment` for medication / contact-book /
   etc by adding ELSIF branches; that keeps each Attachment owner_type behind
   its own ownership predicate without exploding the policy SQL.
3. neutral table GRANTs (holidays, workday_overrides) — read-only, no RLS,
   since these are reference data with no parent ownership semantics.

# Spike → Phase 0 → Phase 1 → Phase 1b 累計範圍
- Phase 0 (parlsr001): 4 roles, guardians covering index
- Phase 1 (parlsr002): student_attendances + guardians RLS (read-only)
- Phase 1b (this): student_leave_requests + attachments RLS + neutral tables

# Design note: ATTACHMENT_DELETE
We GRANT only SELECT/INSERT/UPDATE on attachments (no DELETE). Parents soft-delete
by UPDATE setting `deleted_at`. Application code (`_load_leave_attachments`) already
filters `deleted_at IS NULL`. This keeps the audit trail intact even for parent
actions and avoids the row disappearing from RLS USING after the DELETE.

# Design note: STUDENT_ATTENDANCES_DELETE
We DO GRANT DELETE on student_attendances. Reason: `revert_attendance_for_leave`
hard-deletes the auto-applied attendance rows on cancel — that's the existing
behavior since pre-RLS, and changing to soft-delete would have downstream
implications on the salary engine (attendance counts) and audit log. RLS policy
gates the row scope per parent; advisor confirmed this is safe.

Updates `§6 design principle "no DELETE for parent"` (was conservative; the real
rule is "DELETE requires correctly-scoped policy", which is satisfied here).

# Helper function
`parent_owns_attachment(owner_type, owner_id) → boolean` is SECURITY INVOKER
(default), meaning it runs as the calling role (`ivy_parent_role`) and inherits
the SET LOCAL `app.current_user_id`. Inside, it queries student_leave_requests
+ guardians which themselves have RLS — but those policies match the same
user_id, so the function correctly evaluates "does this attachment belong to
my child". A side effect: if Phase 4 adds another owner_type whose source
table doesn't yet have RLS, the function would see unrestricted rows — so
each new ELSIF branch should only land WITH its source table's RLS.
"""

from __future__ import annotations

from alembic import op

revision = "parlsr003"
down_revision = "parlsr002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. GRANT (writes on phase-1-targeted tables + new tables) ─────────
    op.execute("""
        GRANT SELECT, INSERT, UPDATE ON student_leave_requests TO ivy_parent_role;
        GRANT INSERT, UPDATE, DELETE ON student_attendances TO ivy_parent_role;
        GRANT SELECT, INSERT, UPDATE ON attachments TO ivy_parent_role;
        GRANT SELECT ON holidays, workday_overrides TO ivy_parent_role;

        GRANT USAGE ON SEQUENCE student_leave_requests_id_seq TO ivy_parent_role;
        GRANT USAGE ON SEQUENCE attachments_id_seq TO ivy_parent_role;
        """)

    # ── 2. parent_owns_attachment helper function (Class D polymorphic) ───
    # SECURITY INVOKER (default) — inherits SET LOCAL from caller's tx, so
    # nested queries against guardians / student_leave_requests respect RLS.
    # STABLE = does not modify DB, can be cached within a single statement.
    op.execute("""
        CREATE OR REPLACE FUNCTION parent_owns_attachment(
            p_owner_type text,
            p_owner_id   int
        )
        RETURNS boolean
        LANGUAGE plpgsql
        STABLE
        AS $$
        DECLARE
            uid int;
        BEGIN
            uid := NULLIF(current_setting('app.current_user_id', true), '')::int;
            IF uid IS NULL THEN
                RETURN false;
            END IF;

            -- Phase 1b: only student_leave is gated here. Other owner_types
            -- fall through and return false (fail-closed). Future phases
            -- extend this with additional ELSIF branches per owner_type,
            -- landing together with their source-table RLS policies.
            IF p_owner_type = 'student_leave' THEN
                RETURN EXISTS (
                    SELECT 1
                    FROM student_leave_requests slr
                    JOIN guardians g ON g.student_id = slr.student_id
                    WHERE slr.id = p_owner_id
                      AND g.user_id = uid
                      AND g.deleted_at IS NULL
                );
            END IF;

            RETURN false;
        END
        $$;
        """)

    # ── 3. ENABLE + FORCE RLS on the new tables ───────────────────────────
    op.execute("""
        ALTER TABLE student_leave_requests ENABLE ROW LEVEL SECURITY;
        ALTER TABLE student_leave_requests FORCE ROW LEVEL SECURITY;
        ALTER TABLE attachments ENABLE ROW LEVEL SECURITY;
        ALTER TABLE attachments FORCE ROW LEVEL SECURITY;
        """)

    # ── 4. Policies ────────────────────────────────────────────────────────
    # Class A on student_leave_requests
    op.execute("""
        CREATE POLICY parent_isolate_leave_requests ON student_leave_requests
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
        )
        """)

    # Class D on attachments via the helper function
    op.execute("""
        CREATE POLICY parent_isolate_attachment ON attachments
        FOR ALL TO ivy_parent_role
        USING (parent_owns_attachment(owner_type, owner_id))
        WITH CHECK (parent_owns_attachment(owner_type, owner_id))
        """)

    # holidays / workday_overrides are NEUTRAL reference data — no RLS,
    # GRANT SELECT covers them globally. Already issued in step 1.


def downgrade() -> None:
    # ── 4. Policies ────────────────────────────────────────────────────────
    op.execute("DROP POLICY IF EXISTS parent_isolate_attachment ON attachments")
    op.execute(
        "DROP POLICY IF EXISTS parent_isolate_leave_requests ON student_leave_requests"
    )

    # ── 3. DISABLE RLS ─────────────────────────────────────────────────────
    op.execute("""
        ALTER TABLE attachments NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE attachments DISABLE ROW LEVEL SECURITY;
        ALTER TABLE student_leave_requests NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE student_leave_requests DISABLE ROW LEVEL SECURITY;
        """)

    # ── 2. Helper function ────────────────────────────────────────────────
    op.execute("DROP FUNCTION IF EXISTS parent_owns_attachment(text, int)")

    # ── 1. REVOKE ──────────────────────────────────────────────────────────
    op.execute("""
        REVOKE SELECT ON holidays, workday_overrides FROM ivy_parent_role;
        REVOKE SELECT, INSERT, UPDATE ON attachments FROM ivy_parent_role;
        REVOKE INSERT, UPDATE, DELETE ON student_attendances FROM ivy_parent_role;
        REVOKE SELECT, INSERT, UPDATE ON student_leave_requests FROM ivy_parent_role;
        REVOKE USAGE ON SEQUENCE student_leave_requests_id_seq FROM ivy_parent_role;
        REVOKE USAGE ON SEQUENCE attachments_id_seq FROM ivy_parent_role;
        """)
