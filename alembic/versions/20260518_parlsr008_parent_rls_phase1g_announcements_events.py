"""parent_rls_phase1g_announcements_events: announcements + events + ack tables

Revision ID: parlsr008
Revises: parlsr007
Create Date: 2026-05-18

Phase 1g — `announcements.py` + `events.py` 切到 parent_engine 的 DB 配套。

Six tables touched:
- `announcements` (catalog, NO RLS — admin-managed; app-layer scope filter via
  AnnouncementParentRecipient is sufficient)
- `announcement_parent_recipients` (scope rules, NO RLS — public to all parents)
- `classrooms` (catalog, NO RLS — referenced by scope='classroom')
- `school_events` (catalog, NO RLS — admin-managed event list)
- `announcement_parent_reads` (Class B direct user_id, parent INSERTs on mark-read)
- `event_acknowledgments` (Class B **hybrid** user_id + student_id —
  WITH CHECK must validate BOTH dimensions; this is the spike §2 forge-student gap)

Plus `parent_owns_attachment` gains ELSIF `'event_acknowledgment'` (signature
PDFs uploaded as evidence on event ack).

# Why announcements/school_events stay catalog-only (no RLS)
Both are admin-published global content. App-layer visibility filtering already
handles "which announcement is for whom" (via AnnouncementParentRecipient
scope rules — global/classroom/student/guardian). RLS on top would have to
mirror the entire scope logic in SQL, adding complexity without meaningful
defense-in-depth (admin can publish to "all" and that's a feature, not a leak).

# Why event_acknowledgments needs hybrid WITH CHECK
Schema has both `user_id` (which parent acked) and `student_id` (whose ack it is).
A naive policy `USING (user_id = current_user_id)` doesn't stop forge attacks
on the student dimension: parent A could INSERT row with user_id=A, student_id=B
and create a fake "B's parent acked this event" record. The WITH CHECK adds
the second predicate: `student_id IN guardians of current_user_id`.
"""

from __future__ import annotations

from alembic import op

revision = "parlsr008"
down_revision = "parlsr007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. GRANT ───────────────────────────────────────────────────────────
    op.execute("""
        -- Public catalog tables (no RLS)
        GRANT SELECT ON announcements                  TO ivy_parent_role;
        GRANT SELECT ON announcement_parent_recipients TO ivy_parent_role;
        GRANT SELECT ON classrooms                     TO ivy_parent_role;
        GRANT SELECT ON school_events                  TO ivy_parent_role;

        -- Ack tables (Class B)
        GRANT SELECT, INSERT ON announcement_parent_reads TO ivy_parent_role;
        GRANT SELECT, INSERT, UPDATE ON event_acknowledgments
            TO ivy_parent_role;

        GRANT USAGE ON SEQUENCE announcement_parent_reads_id_seq
            TO ivy_parent_role;
        GRANT USAGE ON SEQUENCE event_acknowledgments_id_seq
            TO ivy_parent_role;
        """)

    # ── 2. ENABLE + FORCE RLS (only on the ack tables) ────────────────────
    for tbl in ("announcement_parent_reads", "event_acknowledgments"):
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE  ROW LEVEL SECURITY")

    # ── 3. Policies ────────────────────────────────────────────────────────
    # announcement_parent_reads — Class B direct user_id (no hybrid; the
    # announcement_id field references admin content that all parents can read)
    op.execute("""
        CREATE POLICY parent_isolate_announcement_parent_reads
        ON announcement_parent_reads
        FOR ALL TO ivy_parent_role
        USING (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
        )
        WITH CHECK (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
        )
        """)

    # event_acknowledgments — Class B HYBRID: user_id AND student_id both
    # checked. Without the student_id predicate, parent A could forge
    # row(user_id=A, student_id=B's_kid). With it, WITH CHECK fails.
    # (Spike design §2 forge-student gap — first real-world exercise of this
    # safeguard for hybrid Class B tables.)
    op.execute("""
        CREATE POLICY parent_isolate_event_acknowledgments
        ON event_acknowledgments
        FOR ALL TO ivy_parent_role
        USING (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
        )
        WITH CHECK (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
            AND student_id IN (
                SELECT g.student_id FROM guardians g
                WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                  AND g.deleted_at IS NULL
            )
        )
        """)

    # ── 4. parent_owns_attachment: add ELSIF 'event_acknowledgment' ───────
    # Existing function has student_leave / contact_book_entry / medication_order
    # branches; this adds the 4th for event ack signature attachments.
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
                -- event_acknowledgments.user_id is sufficient (no need to
                -- re-check student_id at the function layer; the ack table's
                -- own RLS already enforces both dimensions).
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


def downgrade() -> None:
    # ── 4. Restore parent_owns_attachment to Phase 1e body (3 ELSIF) ──────
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
            END IF;

            RETURN false;
        END
        $$;
        """)

    # ── 3. Drop policies ──────────────────────────────────────────────────
    op.execute(
        "DROP POLICY IF EXISTS parent_isolate_event_acknowledgments ON event_acknowledgments"
    )
    op.execute(
        "DROP POLICY IF EXISTS parent_isolate_announcement_parent_reads "
        "ON announcement_parent_reads"
    )

    # ── 2. DISABLE RLS ────────────────────────────────────────────────────
    for tbl in ("event_acknowledgments", "announcement_parent_reads"):
        op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} DISABLE  ROW LEVEL SECURITY")

    # ── 1. REVOKE ─────────────────────────────────────────────────────────
    op.execute("""
        REVOKE USAGE ON SEQUENCE event_acknowledgments_id_seq
            FROM ivy_parent_role;
        REVOKE USAGE ON SEQUENCE announcement_parent_reads_id_seq
            FROM ivy_parent_role;

        REVOKE SELECT, INSERT, UPDATE ON event_acknowledgments
            FROM ivy_parent_role;
        REVOKE SELECT, INSERT ON announcement_parent_reads FROM ivy_parent_role;

        REVOKE SELECT ON school_events                  FROM ivy_parent_role;
        REVOKE SELECT ON classrooms                     FROM ivy_parent_role;
        REVOKE SELECT ON announcement_parent_recipients FROM ivy_parent_role;
        REVOKE SELECT ON announcements                  FROM ivy_parent_role;
        """)
