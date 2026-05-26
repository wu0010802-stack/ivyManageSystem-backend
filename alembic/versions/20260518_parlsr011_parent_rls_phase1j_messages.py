"""parent_rls_phase1j_messages: parent_message_threads + parent_messages

Revision ID: parlsr011
Revises: parlsr010
Create Date: 2026-05-18

Phase 1j (final) — `messages.py` + `home.py` + `parent_downloads.py` 切到
parent_engine 的 DB 配套。Phase 1j 結束後，**所有 parent_portal/ 內適用 RLS
的 router 都已切完**（assistant 純 no-DB 跳過、binding_admin 是 staff 端點
不切，總計 19/25 = 76%；其餘 6 router 為 auth/_shared/_dependencies/__init__/
internal helper 不算 router endpoint）。

# Two tables RLS-enabled

## parent_message_threads (Class B direct parent_user_id)
- Parent reads own threads + UPDATE parent_last_read_at on mark-read
- WITH CHECK mirrors USING — parent can't forge thread to point elsewhere

## parent_messages (Class C via thread_id JOIN)
- Indirect ownership via `parent_message_threads.parent_user_id`
- Parent INSERTs own replies; UPDATEs `deleted_at` on recall (30-min window)
- WITH CHECK enforces thread_id belongs to current parent
- Note: sender_user_id check at app-layer (must = current_user_id for INSERT);
  RLS only validates thread ownership

# parent_owns_attachment gains 7th ELSIF 'message'

Final extension. Function now recognises 7 owner_types:
`student_leave` / `contact_book_entry` / `medication_order` /
`event_acknowledgment` / `observation` / `report` / `message`.

For 'message': owner_id is `parent_messages.id`; need to JOIN to
`parent_message_threads.parent_user_id` to verify ownership.

Phase 4 (Attachment polymorphic) is effectively complete with this migration —
all known owner_types are now gated. Unknown owner_types remain fail-closed.
"""

from __future__ import annotations

from alembic import op

revision = "parlsr011"
down_revision = "parlsr010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. GRANT ───────────────────────────────────────────────────────────
    op.execute("""
        GRANT SELECT, UPDATE ON parent_message_threads TO ivy_parent_role;
        GRANT SELECT, INSERT, UPDATE ON parent_messages TO ivy_parent_role;

        GRANT USAGE ON SEQUENCE parent_message_threads_id_seq TO ivy_parent_role;
        GRANT USAGE ON SEQUENCE parent_messages_id_seq TO ivy_parent_role;
        """)

    # ── 2. ENABLE + FORCE RLS ─────────────────────────────────────────────
    for tbl in ("parent_message_threads", "parent_messages"):
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE  ROW LEVEL SECURITY")

    # ── 3. Policies ────────────────────────────────────────────────────────
    # parent_message_threads — Class B direct parent_user_id, FOR ALL
    op.execute("""
        CREATE POLICY parent_isolate_parent_message_threads
        ON parent_message_threads
        FOR ALL TO ivy_parent_role
        USING (
            parent_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
        )
        WITH CHECK (
            parent_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
        )
        """)

    # parent_messages — Class C via thread_id JOIN, FOR ALL
    # Note: WITH CHECK ensures the row's thread_id belongs to current parent's
    # threads. sender_user_id self-check stays at app layer (the router enforces
    # sender == current_user before INSERT).
    op.execute("""
        CREATE POLICY parent_isolate_parent_messages
        ON parent_messages
        FOR ALL TO ivy_parent_role
        USING (
            thread_id IN (
                SELECT t.id FROM parent_message_threads t
                WHERE t.parent_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
            )
        )
        WITH CHECK (
            thread_id IN (
                SELECT t.id FROM parent_message_threads t
                WHERE t.parent_user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
            )
        )
        """)

    # ── 4. parent_owns_attachment: add 7th ELSIF 'message' ────────────────
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
            ELSIF p_owner_type = 'message' THEN
                -- Message attachment: owner_id is parent_messages.id;
                -- JOIN to parent_message_threads to check parent_user_id.
                RETURN EXISTS (
                    SELECT 1 FROM parent_messages m
                    JOIN parent_message_threads t ON t.id = m.thread_id
                    WHERE m.id = p_owner_id
                      AND t.parent_user_id = uid
                );
            END IF;

            RETURN false;
        END
        $$;
        """)


def downgrade() -> None:
    # ── 4. Restore parent_owns_attachment to Phase 1i body (6 ELSIF) ──────
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

    # ── 3. Drop policies ──────────────────────────────────────────────────
    op.execute(
        "DROP POLICY IF EXISTS parent_isolate_parent_messages ON parent_messages"
    )
    op.execute(
        "DROP POLICY IF EXISTS parent_isolate_parent_message_threads "
        "ON parent_message_threads"
    )

    # ── 2. DISABLE RLS ────────────────────────────────────────────────────
    for tbl in ("parent_messages", "parent_message_threads"):
        op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} DISABLE  ROW LEVEL SECURITY")

    # ── 1. REVOKE ─────────────────────────────────────────────────────────
    # 用 DO block 偵測 ivy_parent_role 存在才 REVOKE，支援 Alembic Roundtrip CI
    # 的 stamp-only DB（沒跑 parlsr001 建立 role）。
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ivy_parent_role') THEN
                REVOKE USAGE ON SEQUENCE parent_messages_id_seq FROM ivy_parent_role;
                REVOKE USAGE ON SEQUENCE parent_message_threads_id_seq FROM ivy_parent_role;
                REVOKE SELECT, INSERT, UPDATE ON parent_messages FROM ivy_parent_role;
                REVOKE SELECT, UPDATE ON parent_message_threads FROM ivy_parent_role;
            END IF;
        END $$;
        """)
