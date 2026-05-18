"""parent_rls_phase1f_activity_students: activity router + retro students RLS

Revision ID: parlsr007
Revises: parlsr006
Create Date: 2026-05-18

Phase 1f — `activity.py` 切到 parent_engine + retroactive fix for `students` RLS.

# Two halves

## 1. Retroactive fix: students RLS

Phases 1b (leaves.py) and 1e (medications.py) use `_assert_student_owned(for_write=True)`
which reads `students.lifecycle_status` to gate writes on terminal-state children.
Before this migration, `students` had no GRANT to `ivy_parent_role` — so the prod
path would 500 with `permission denied for table students`. Existing test suites
didn't catch it because:
- spike_rls tests INSERT/SELECT directly without going through `_assert_student_owned`
- SQLite override tests bypass PG's permission system

Phase 1f migration adds `GRANT SELECT ON students` + `ENABLE+FORCE RLS` + Class A
policy on `students.id` matched via `guardians`. Parent sees only own children's
rows; admin (BYPASSRLS) sees all.

## 2. Activity router: registrations + 3 subresources + audit log + catalog

Tables RLS-enabled:
- `activity_registrations` (Class A direct on `student_id`)
- `registration_courses` (Class A via `registration_id` → activity_registrations)
- `registration_supplies` (Class A via `registration_id`)
- `activity_payment_records` (Class A via `registration_id`, SELECT-only)
- `registration_changes` (Class A via `registration_id`, SELECT+INSERT; NULL
  registration_id rows are admin-side global logs, parent never sees those)

Tables GRANT SELECT only, NO RLS (public catalog):
- `activity_courses` (course catalog)
- `activity_supplies` (supply catalog)
These are public — every parent sees them all. RLS would be wrong shape.

# Edge: NULL registration_id in registration_changes
The column is nullable; admin-side global registration_change rows have NULL
registration_id. The policy `registration_id IN (parent's regs)` evaluates to
FALSE for NULL (per SQL three-valued logic on IN), so NULL rows are correctly
hidden from parent — that's the fail-closed intent.
"""

from __future__ import annotations

from alembic import op

revision = "parlsr007"
down_revision = "parlsr006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. GRANT (retro students + activity batch) ────────────────────────
    op.execute("""
        -- Retroactive: students
        GRANT SELECT ON students TO ivy_parent_role;

        -- Public catalog (no RLS, every parent reads)
        GRANT SELECT ON activity_courses              TO ivy_parent_role;
        GRANT SELECT ON activity_supplies             TO ivy_parent_role;

        -- Registration main + subresources
        GRANT SELECT, INSERT, UPDATE ON activity_registrations
            TO ivy_parent_role;
        GRANT SELECT, INSERT, UPDATE ON registration_courses
            TO ivy_parent_role;
        GRANT SELECT, INSERT ON registration_supplies TO ivy_parent_role;
        GRANT SELECT ON activity_payment_records      TO ivy_parent_role;
        GRANT SELECT, INSERT ON registration_changes  TO ivy_parent_role;

        -- Sequence USAGE for INSERT-target tables
        GRANT USAGE ON SEQUENCE activity_registrations_id_seq
            TO ivy_parent_role;
        GRANT USAGE ON SEQUENCE registration_courses_id_seq
            TO ivy_parent_role;
        GRANT USAGE ON SEQUENCE registration_supplies_id_seq
            TO ivy_parent_role;
        GRANT USAGE ON SEQUENCE registration_changes_id_seq
            TO ivy_parent_role;
        """)

    # ── 2. ENABLE + FORCE RLS ─────────────────────────────────────────────
    for tbl in (
        "students",
        "activity_registrations",
        "registration_courses",
        "registration_supplies",
        "activity_payment_records",
        "registration_changes",
    ):
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE  ROW LEVEL SECURITY")

    # ── 3. Policies ────────────────────────────────────────────────────────
    # students — Class A direct on `id`. Parent sees own kids' rows.
    op.execute("""
        CREATE POLICY parent_isolate_students ON students
        FOR SELECT TO ivy_parent_role
        USING (
            id IN (
                SELECT g.student_id FROM guardians g
                WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                  AND g.deleted_at IS NULL
            )
        )
        """)

    # activity_registrations — Class A direct on student_id
    op.execute("""
        CREATE POLICY parent_isolate_activity_registrations ON activity_registrations
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

    # ── 4. Helper function: count enrolled per course (admin-bypass) ────
    # Parents need to see "is_full" / occupancy on the public course catalog,
    # but `registration_courses` is RLS-scoped to the calling parent — direct
    # COUNT would return only their own row, giving wrong is_full. SECURITY
    # DEFINER runs as function owner (typically the migration runner /
    # superuser), bypassing RLS so the count includes every parent's row.
    # The function only returns a count, not row IDs — no real-row leak.
    op.execute("""
        CREATE OR REPLACE FUNCTION public_count_enrolled(p_course_id int)
        RETURNS bigint
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        AS $$
            SELECT count(*) FROM registration_courses
            WHERE course_id = p_course_id
              AND status IN ('enrolled', 'promoted_pending');
        $$;
        GRANT EXECUTE ON FUNCTION public_count_enrolled(int) TO ivy_parent_role;
        """)

    # registration_courses / registration_supplies / activity_payment_records /
    # registration_changes — all Class A indirect via registration_id JOIN.
    for table_name, policy_name, cmd in (
        ("registration_courses", "parent_isolate_registration_courses", "FOR ALL"),
        ("registration_supplies", "parent_isolate_registration_supplies", "FOR ALL"),
        (
            "activity_payment_records",
            "parent_isolate_activity_payment_records",
            "FOR SELECT",
        ),
        ("registration_changes", "parent_isolate_registration_changes", "FOR ALL"),
    ):
        # Build USING + (for FOR ALL) WITH CHECK
        join_predicate = """
            registration_id IN (
                SELECT r.id FROM activity_registrations r
                JOIN guardians g ON g.student_id = r.student_id
                WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                  AND g.deleted_at IS NULL
            )
        """
        if cmd == "FOR ALL":
            op.execute(f"""
                CREATE POLICY {policy_name} ON {table_name}
                {cmd} TO ivy_parent_role
                USING ({join_predicate})
                WITH CHECK ({join_predicate})
                """)
        else:
            op.execute(f"""
                CREATE POLICY {policy_name} ON {table_name}
                {cmd} TO ivy_parent_role
                USING ({join_predicate})
                """)


def downgrade() -> None:
    # ── 4. Drop helper function ───────────────────────────────────────────
    op.execute("DROP FUNCTION IF EXISTS public_count_enrolled(int)")

    # ── 3. Policies ────────────────────────────────────────────────────────
    for table_name, policy_name in (
        ("registration_changes", "parent_isolate_registration_changes"),
        ("activity_payment_records", "parent_isolate_activity_payment_records"),
        ("registration_supplies", "parent_isolate_registration_supplies"),
        ("registration_courses", "parent_isolate_registration_courses"),
        ("activity_registrations", "parent_isolate_activity_registrations"),
        ("students", "parent_isolate_students"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table_name}")

    # ── 2. DISABLE RLS ────────────────────────────────────────────────────
    for tbl in (
        "registration_changes",
        "activity_payment_records",
        "registration_supplies",
        "registration_courses",
        "activity_registrations",
        "students",
    ):
        op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} DISABLE  ROW LEVEL SECURITY")

    # ── 1. REVOKE ─────────────────────────────────────────────────────────
    op.execute("""
        REVOKE USAGE ON SEQUENCE registration_changes_id_seq    FROM ivy_parent_role;
        REVOKE USAGE ON SEQUENCE registration_supplies_id_seq   FROM ivy_parent_role;
        REVOKE USAGE ON SEQUENCE registration_courses_id_seq    FROM ivy_parent_role;
        REVOKE USAGE ON SEQUENCE activity_registrations_id_seq  FROM ivy_parent_role;

        REVOKE SELECT, INSERT ON registration_changes           FROM ivy_parent_role;
        REVOKE SELECT ON activity_payment_records               FROM ivy_parent_role;
        REVOKE SELECT, INSERT ON registration_supplies          FROM ivy_parent_role;
        REVOKE SELECT, INSERT, UPDATE ON registration_courses   FROM ivy_parent_role;
        REVOKE SELECT, INSERT, UPDATE ON activity_registrations FROM ivy_parent_role;
        REVOKE SELECT ON activity_supplies                      FROM ivy_parent_role;
        REVOKE SELECT ON activity_courses                       FROM ivy_parent_role;

        REVOKE SELECT ON students                               FROM ivy_parent_role;
        """)
