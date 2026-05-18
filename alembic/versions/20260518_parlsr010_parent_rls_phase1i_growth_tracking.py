"""parent_rls_phase1i_growth_tracking: timeline + notifications + home + photos

Revision ID: parlsr010
Revises: parlsr009
Create Date: 2026-05-18

Phase 1i — `timeline.py` + `notifications.py` + `home.py` + `photos.py` 切到
parent_engine 的 DB 配套。

Note: `binding_admin.py` is mis-located in `api/parent_portal/` but uses
`require_staff_permission` — it's a staff endpoint, NOT parent. Skipped.

# Six tables RLS-enabled

## Class A direct (5 read-only tables)
- `student_assessments` — semester evaluations
- `student_incidents` — health/behavioral events
- `student_observations` — teacher daily observations
- `parent_communication_logs` — teacher↔parent comms log
- `student_dismissal_calls` — dismissal queue history

## Class B direct user_id (1 read+write table)
- `parent_notification_preferences` — parent toggles per-channel notif on/off

# parent_owns_attachment gains 2 ELSIF

- `'observation'` (ATTACHMENT_OWNER_OBSERVATION) — observation photo gallery
- `'report'` (ATTACHMENT_OWNER_REPORT) — growth report PDF attachments

After this migration the function recognises 6 owner_types total:
`student_leave` / `contact_book_entry` / `medication_order` /
`event_acknowledgment` / `observation` / `report`. Unknown owner_types remain
fail-closed (RETURN false).
"""

from __future__ import annotations

from alembic import op

revision = "parlsr010"
down_revision = "parlsr009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. GRANT ───────────────────────────────────────────────────────────
    op.execute("""
        GRANT SELECT ON student_assessments        TO ivy_parent_role;
        GRANT SELECT ON student_incidents          TO ivy_parent_role;
        GRANT SELECT ON student_observations       TO ivy_parent_role;
        GRANT SELECT ON parent_communication_logs  TO ivy_parent_role;
        GRANT SELECT ON student_dismissal_calls    TO ivy_parent_role;

        GRANT SELECT, INSERT, UPDATE ON parent_notification_preferences
            TO ivy_parent_role;

        GRANT USAGE ON SEQUENCE parent_notification_preferences_id_seq
            TO ivy_parent_role;
        """)

    # ── 2. ENABLE + FORCE RLS ─────────────────────────────────────────────
    for tbl in (
        "student_assessments",
        "student_incidents",
        "student_observations",
        "parent_communication_logs",
        "student_dismissal_calls",
        "parent_notification_preferences",
    ):
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE  ROW LEVEL SECURITY")

    # ── 3. Class A direct policies (5 read-only tables) ───────────────────
    for table_name in (
        "student_assessments",
        "student_incidents",
        "student_observations",
        "parent_communication_logs",
        "student_dismissal_calls",
    ):
        op.execute(f"""
            CREATE POLICY parent_isolate_{table_name} ON {table_name}
            FOR SELECT TO ivy_parent_role
            USING (
                student_id IN (
                    SELECT g.student_id FROM guardians g
                    WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                      AND g.deleted_at IS NULL
                )
            )
            """)

    # ── 4. Class B direct policy on parent_notification_preferences ───────
    op.execute("""
        CREATE POLICY parent_isolate_parent_notification_preferences
        ON parent_notification_preferences
        FOR ALL TO ivy_parent_role
        USING (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
        )
        WITH CHECK (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
        )
        """)

    # ── 5. parent_owns_attachment: extend with 'observation' + 'report' ───
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

            IF p_owner_type = 'student_leave' THEN
                RETURN EXISTS (
                    SELECT 1 FROM student_leave_requests slr
                    JOIN guardians g ON g.student_id = slr.student_id
                    WHERE slr.id = p_owner_id
                      AND g.user_id = uid
                      AND g.deleted_at IS NULL
                );
            ELSIF p_owner_type = 'contact_book_entry' THEN
                RETURN EXISTS (
                    SELECT 1 FROM student_contact_book_entries e
                    JOIN guardians g ON g.student_id = e.student_id
                    WHERE e.id = p_owner_id
                      AND g.user_id = uid
                      AND g.deleted_at IS NULL
                );
            ELSIF p_owner_type = 'medication_order' THEN
                RETURN EXISTS (
                    SELECT 1 FROM student_medication_orders o
                    JOIN guardians g ON g.student_id = o.student_id
                    WHERE o.id = p_owner_id
                      AND g.user_id = uid
                      AND g.deleted_at IS NULL
                );
            ELSIF p_owner_type = 'event_acknowledgment' THEN
                RETURN EXISTS (
                    SELECT 1 FROM event_acknowledgments ea
                    WHERE ea.id = p_owner_id
                      AND ea.user_id = uid
                );
            ELSIF p_owner_type = 'observation' THEN
                RETURN EXISTS (
                    SELECT 1 FROM student_observations o
                    JOIN guardians g ON g.student_id = o.student_id
                    WHERE o.id = p_owner_id
                      AND g.user_id = uid
                      AND g.deleted_at IS NULL
                );
            ELSIF p_owner_type = 'report' THEN
                RETURN EXISTS (
                    SELECT 1 FROM student_growth_reports r
                    JOIN guardians g ON g.student_id = r.student_id
                    WHERE r.id = p_owner_id
                      AND g.user_id = uid
                      AND g.deleted_at IS NULL
                );
            END IF;

            RETURN false;
        END
        $$;
        """)


def downgrade() -> None:
    # ── 5. Restore parent_owns_attachment to Phase 1g body (4 ELSIF) ──────
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

            IF p_owner_type = 'student_leave' THEN
                RETURN EXISTS (
                    SELECT 1 FROM student_leave_requests slr
                    JOIN guardians g ON g.student_id = slr.student_id
                    WHERE slr.id = p_owner_id
                      AND g.user_id = uid
                      AND g.deleted_at IS NULL
                );
            ELSIF p_owner_type = 'contact_book_entry' THEN
                RETURN EXISTS (
                    SELECT 1 FROM student_contact_book_entries e
                    JOIN guardians g ON g.student_id = e.student_id
                    WHERE e.id = p_owner_id
                      AND g.user_id = uid
                      AND g.deleted_at IS NULL
                );
            ELSIF p_owner_type = 'medication_order' THEN
                RETURN EXISTS (
                    SELECT 1 FROM student_medication_orders o
                    JOIN guardians g ON g.student_id = o.student_id
                    WHERE o.id = p_owner_id
                      AND g.user_id = uid
                      AND g.deleted_at IS NULL
                );
            ELSIF p_owner_type = 'event_acknowledgment' THEN
                RETURN EXISTS (
                    SELECT 1 FROM event_acknowledgments ea
                    WHERE ea.id = p_owner_id
                      AND ea.user_id = uid
                );
            END IF;

            RETURN false;
        END
        $$;
        """)

    # ── 4 + 3. Drop policies ──────────────────────────────────────────────
    op.execute(
        "DROP POLICY IF EXISTS parent_isolate_parent_notification_preferences "
        "ON parent_notification_preferences"
    )
    for table_name in (
        "student_dismissal_calls",
        "parent_communication_logs",
        "student_observations",
        "student_incidents",
        "student_assessments",
    ):
        op.execute(f"DROP POLICY IF EXISTS parent_isolate_{table_name} ON {table_name}")

    # ── 2. DISABLE RLS ────────────────────────────────────────────────────
    for tbl in (
        "parent_notification_preferences",
        "student_dismissal_calls",
        "parent_communication_logs",
        "student_observations",
        "student_incidents",
        "student_assessments",
    ):
        op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} DISABLE  ROW LEVEL SECURITY")

    # ── 1. REVOKE ─────────────────────────────────────────────────────────
    op.execute("""
        REVOKE USAGE ON SEQUENCE parent_notification_preferences_id_seq
            FROM ivy_parent_role;

        REVOKE SELECT, INSERT, UPDATE ON parent_notification_preferences
            FROM ivy_parent_role;

        REVOKE SELECT ON student_dismissal_calls   FROM ivy_parent_role;
        REVOKE SELECT ON parent_communication_logs FROM ivy_parent_role;
        REVOKE SELECT ON student_observations      FROM ivy_parent_role;
        REVOKE SELECT ON student_incidents         FROM ivy_parent_role;
        REVOKE SELECT ON student_assessments       FROM ivy_parent_role;
        """)
