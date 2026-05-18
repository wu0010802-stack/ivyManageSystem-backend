"""parent_rls_phase1e_contact_book_meds: contact_book + medications + polymorphic ELSIF

Revision ID: parlsr006
Revises: parlsr005
Create Date: 2026-05-18

Phase 1e — `contact_book.py` + `medications.py` 切到 parent_engine 的 DB 配套。

Six new RLS-enabled tables + two new ELSIF branches on `parent_owns_attachment`:
1. `student_contact_book_entries` (Class A direct, SELECT-only — admin writes)
2. `student_contact_book_acks` (Class A via `entry_id`, SELECT+INSERT)
3. `student_contact_book_replies` (Class A via `entry_id`, SELECT+INSERT+UPDATE
   for soft-delete via `deleted_at`)
4. `student_medication_orders` (Class A direct, SELECT+INSERT+UPDATE)
5. `student_medication_logs` (Class A via `order_id`, SELECT+INSERT)
6. `student_allergies` (Class A direct, SELECT-only — read by
   `find_allergy_conflicts` service helper from POST /medication-orders)

Plus `parent_owns_attachment` extended with two new owner_types to satisfy the
existing Class D polymorphic policy on `attachments`:
- `'contact_book_entry'` → JOIN `student_contact_book_entries`
- `'medication_order'` → JOIN `student_medication_orders`

# Why student_contact_book_entries is SELECT-only
Admin teachers create entries; parents only read + ack + reply. The
contact_book_acks / replies subresources carry the parent's writes. INSERT
or UPDATE on entries from the parent role would be a misuse — GRANT just
SELECT and let the policy stay tight.

# Why student_allergies is SELECT-only
Allergies are managed in admin / portfolio flows. Parent endpoint only reads
them to detect medication-vs-allergen conflicts at order time
(`find_allergy_conflicts`). No parent write surface; GRANT just SELECT.

# Downgrade ELSIF restore
`parent_owns_attachment` is CREATE OR REPLACE'd back to the Phase 1b body
(student_leave only) — DROP would break the existing attachments policy
which still references the function.
"""

from __future__ import annotations

from alembic import op

revision = "parlsr006"
down_revision = "parlsr005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. GRANT ───────────────────────────────────────────────────────────
    op.execute("""
        GRANT SELECT ON student_contact_book_entries          TO ivy_parent_role;
        GRANT SELECT, INSERT ON student_contact_book_acks     TO ivy_parent_role;
        GRANT SELECT, INSERT, UPDATE ON student_contact_book_replies
            TO ivy_parent_role;

        GRANT SELECT, INSERT, UPDATE ON student_medication_orders
            TO ivy_parent_role;
        GRANT SELECT, INSERT ON student_medication_logs       TO ivy_parent_role;

        GRANT SELECT ON student_allergies                     TO ivy_parent_role;

        GRANT USAGE ON SEQUENCE student_contact_book_acks_id_seq
            TO ivy_parent_role;
        GRANT USAGE ON SEQUENCE student_contact_book_replies_id_seq
            TO ivy_parent_role;
        GRANT USAGE ON SEQUENCE student_medication_orders_id_seq
            TO ivy_parent_role;
        GRANT USAGE ON SEQUENCE student_medication_logs_id_seq
            TO ivy_parent_role;
        """)

    # ── 2. ENABLE + FORCE RLS ─────────────────────────────────────────────
    for tbl in (
        "student_contact_book_entries",
        "student_contact_book_acks",
        "student_contact_book_replies",
        "student_medication_orders",
        "student_medication_logs",
        "student_allergies",
    ):
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE  ROW LEVEL SECURITY")

    # ── 3. Direct Class A policies (student_id column) ────────────────────
    # student_contact_book_entries: FOR SELECT (no INSERT/UPDATE from parent)
    op.execute("""
        CREATE POLICY parent_isolate_student_contact_book_entries
        ON student_contact_book_entries
        FOR SELECT TO ivy_parent_role
        USING (
            student_id IN (
                SELECT g.student_id FROM guardians g
                WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                  AND g.deleted_at IS NULL
            )
        )
        """)
    # student_medication_orders: FOR ALL (parent INSERTs orders)
    op.execute("""
        CREATE POLICY parent_isolate_student_medication_orders
        ON student_medication_orders
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
    # student_allergies: FOR SELECT (parent never writes; admin/portfolio does)
    op.execute("""
        CREATE POLICY parent_isolate_student_allergies
        ON student_allergies
        FOR SELECT TO ivy_parent_role
        USING (
            student_id IN (
                SELECT g.student_id FROM guardians g
                WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                  AND g.deleted_at IS NULL
            )
        )
        """)

    # ── 4. Indirect Class A via entry_id / order_id ───────────────────────
    # contact_book_acks via entry_id → student_contact_book_entries.student_id
    op.execute("""
        CREATE POLICY parent_isolate_student_contact_book_acks
        ON student_contact_book_acks
        FOR ALL TO ivy_parent_role
        USING (
            entry_id IN (
                SELECT e.id FROM student_contact_book_entries e
                JOIN guardians g ON g.student_id = e.student_id
                WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                  AND g.deleted_at IS NULL
            )
        )
        WITH CHECK (
            entry_id IN (
                SELECT e.id FROM student_contact_book_entries e
                JOIN guardians g ON g.student_id = e.student_id
                WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                  AND g.deleted_at IS NULL
            )
        )
        """)
    # contact_book_replies via entry_id
    op.execute("""
        CREATE POLICY parent_isolate_student_contact_book_replies
        ON student_contact_book_replies
        FOR ALL TO ivy_parent_role
        USING (
            entry_id IN (
                SELECT e.id FROM student_contact_book_entries e
                JOIN guardians g ON g.student_id = e.student_id
                WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                  AND g.deleted_at IS NULL
            )
        )
        WITH CHECK (
            entry_id IN (
                SELECT e.id FROM student_contact_book_entries e
                JOIN guardians g ON g.student_id = e.student_id
                WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                  AND g.deleted_at IS NULL
            )
        )
        """)
    # medication_logs via order_id → student_medication_orders.student_id
    op.execute("""
        CREATE POLICY parent_isolate_student_medication_logs
        ON student_medication_logs
        FOR ALL TO ivy_parent_role
        USING (
            order_id IN (
                SELECT o.id FROM student_medication_orders o
                JOIN guardians g ON g.student_id = o.student_id
                WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                  AND g.deleted_at IS NULL
            )
        )
        WITH CHECK (
            order_id IN (
                SELECT o.id FROM student_medication_orders o
                JOIN guardians g ON g.student_id = o.student_id
                WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                  AND g.deleted_at IS NULL
            )
        )
        """)

    # ── 5. parent_owns_attachment: extend with two new owner_types ────────
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
                    SELECT 1
                    FROM student_leave_requests slr
                    JOIN guardians g ON g.student_id = slr.student_id
                    WHERE slr.id = p_owner_id
                      AND g.user_id = uid
                      AND g.deleted_at IS NULL
                );
            ELSIF p_owner_type = 'contact_book_entry' THEN
                RETURN EXISTS (
                    SELECT 1
                    FROM student_contact_book_entries e
                    JOIN guardians g ON g.student_id = e.student_id
                    WHERE e.id = p_owner_id
                      AND g.user_id = uid
                      AND g.deleted_at IS NULL
                );
            ELSIF p_owner_type = 'medication_order' THEN
                RETURN EXISTS (
                    SELECT 1
                    FROM student_medication_orders o
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


def downgrade() -> None:
    # ── 5. Restore parent_owns_attachment to Phase 1b body (student_leave only) ──
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

    # ── 4 + 3. Drop policies ──────────────────────────────────────────────
    for table_name, policy_name in (
        ("student_medication_logs", "parent_isolate_student_medication_logs"),
        ("student_contact_book_replies", "parent_isolate_student_contact_book_replies"),
        ("student_contact_book_acks", "parent_isolate_student_contact_book_acks"),
        ("student_allergies", "parent_isolate_student_allergies"),
        ("student_medication_orders", "parent_isolate_student_medication_orders"),
        ("student_contact_book_entries", "parent_isolate_student_contact_book_entries"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table_name}")

    # ── 2. DISABLE RLS ────────────────────────────────────────────────────
    for tbl in (
        "student_medication_logs",
        "student_contact_book_replies",
        "student_contact_book_acks",
        "student_allergies",
        "student_medication_orders",
        "student_contact_book_entries",
    ):
        op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} DISABLE  ROW LEVEL SECURITY")

    # ── 1. REVOKE ─────────────────────────────────────────────────────────
    op.execute("""
        REVOKE USAGE ON SEQUENCE student_medication_logs_id_seq
            FROM ivy_parent_role;
        REVOKE USAGE ON SEQUENCE student_medication_orders_id_seq
            FROM ivy_parent_role;
        REVOKE USAGE ON SEQUENCE student_contact_book_replies_id_seq
            FROM ivy_parent_role;
        REVOKE USAGE ON SEQUENCE student_contact_book_acks_id_seq
            FROM ivy_parent_role;

        REVOKE SELECT ON student_allergies                     FROM ivy_parent_role;
        REVOKE SELECT, INSERT ON student_medication_logs       FROM ivy_parent_role;
        REVOKE SELECT, INSERT, UPDATE ON student_medication_orders
            FROM ivy_parent_role;
        REVOKE SELECT, INSERT, UPDATE ON student_contact_book_replies
            FROM ivy_parent_role;
        REVOKE SELECT, INSERT ON student_contact_book_acks     FROM ivy_parent_role;
        REVOKE SELECT ON student_contact_book_entries          FROM ivy_parent_role;
        """)
