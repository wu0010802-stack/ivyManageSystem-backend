"""parent_rls_phase1c_reads: fees + measurements + growth_reports

Revision ID: parlsr004
Revises: parlsr003
Create Date: 2026-05-18

Phase 1c — 3 個 read-heavy router 切到 parent_engine 的 DB 配套。

Covers 6 tables across 3 routers:
- `fees.py`              → student_fee_records (direct), student_fee_payments
                           (via record_id), student_fee_adjustments (direct),
                           student_fee_refunds (via record_id). All read-only.
- `measurements.py`      → student_measurements (direct). Read-only.
- `growth_reports.py`    → student_growth_reports (direct). Read + UPDATE
                           (parent_view_count + parent_first_viewed_at atomic
                           bump on download). FOR ALL policy supports UPDATE.

Pattern: each table gets `GRANT SELECT` (plus `UPDATE` only on growth_reports),
`ENABLE + FORCE ROW LEVEL SECURITY`, and a Class A policy that gates rows by
ownership through guardians. Two tables (fee_payments + fee_refunds) don't have
direct `student_id` — they JOIN through `student_fee_records.id`.

# Why FOR ALL on student_growth_reports despite no INSERT/DELETE GRANT
`parent_download_report` atomically UPDATEs view_count via
`UPDATE ... SET col = COALESCE(col, 0) + 1`. Without `FOR ALL` (or explicit
`FOR UPDATE`), Postgres rejects the UPDATE under FORCE RLS even with the GRANT.
We use `FOR ALL` for symmetry with leaves' parent_isolate_leave_requests;
WITH CHECK simply matches USING (the parent isn't changing student_id).
"""

from __future__ import annotations

from alembic import op

revision = "parlsr004"
down_revision = "parlsr003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. GRANT ───────────────────────────────────────────────────────────
    op.execute("""
        GRANT SELECT ON student_fee_records         TO ivy_parent_role;
        GRANT SELECT ON student_fee_payments        TO ivy_parent_role;
        GRANT SELECT ON student_fee_adjustments     TO ivy_parent_role;
        GRANT SELECT ON student_fee_refunds         TO ivy_parent_role;
        GRANT SELECT ON student_measurements        TO ivy_parent_role;
        GRANT SELECT, UPDATE ON student_growth_reports TO ivy_parent_role;
        """)

    # ── 2. ENABLE + FORCE RLS ─────────────────────────────────────────────
    op.execute("""
        ALTER TABLE student_fee_records         ENABLE ROW LEVEL SECURITY;
        ALTER TABLE student_fee_records         FORCE  ROW LEVEL SECURITY;
        ALTER TABLE student_fee_payments        ENABLE ROW LEVEL SECURITY;
        ALTER TABLE student_fee_payments        FORCE  ROW LEVEL SECURITY;
        ALTER TABLE student_fee_adjustments     ENABLE ROW LEVEL SECURITY;
        ALTER TABLE student_fee_adjustments     FORCE  ROW LEVEL SECURITY;
        ALTER TABLE student_fee_refunds         ENABLE ROW LEVEL SECURITY;
        ALTER TABLE student_fee_refunds         FORCE  ROW LEVEL SECURITY;
        ALTER TABLE student_measurements        ENABLE ROW LEVEL SECURITY;
        ALTER TABLE student_measurements        FORCE  ROW LEVEL SECURITY;
        ALTER TABLE student_growth_reports      ENABLE ROW LEVEL SECURITY;
        ALTER TABLE student_growth_reports      FORCE  ROW LEVEL SECURITY;
        """)

    # ── 3. Policies ────────────────────────────────────────────────────────
    # 3a. Direct Class A (student_id column on the row)
    for table_name in (
        "student_fee_records",
        "student_fee_adjustments",
        "student_measurements",
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

    # 3b. student_growth_reports needs FOR ALL because parent_download_report
    # does an atomic UPDATE on view_count. WITH CHECK mirrors USING.
    op.execute("""
        CREATE POLICY parent_isolate_student_growth_reports ON student_growth_reports
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

    # 3c. Indirect Class A via record_id → student_fee_records.student_id
    for table_name in ("student_fee_payments", "student_fee_refunds"):
        op.execute(f"""
            CREATE POLICY parent_isolate_{table_name} ON {table_name}
            FOR SELECT TO ivy_parent_role
            USING (
                record_id IN (
                    SELECT r.id FROM student_fee_records r
                    JOIN guardians g ON g.student_id = r.student_id
                    WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                      AND g.deleted_at IS NULL
                )
            )
            """)


def downgrade() -> None:
    # ── 3. Drop policies ──────────────────────────────────────────────────
    for table_name in (
        "student_fee_records",
        "student_fee_adjustments",
        "student_measurements",
        "student_growth_reports",
        "student_fee_payments",
        "student_fee_refunds",
    ):
        op.execute(f"DROP POLICY IF EXISTS parent_isolate_{table_name} ON {table_name}")

    # ── 2. DISABLE RLS ────────────────────────────────────────────────────
    op.execute("""
        ALTER TABLE student_growth_reports      NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE student_growth_reports      DISABLE  ROW LEVEL SECURITY;
        ALTER TABLE student_measurements        NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE student_measurements        DISABLE  ROW LEVEL SECURITY;
        ALTER TABLE student_fee_refunds         NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE student_fee_refunds         DISABLE  ROW LEVEL SECURITY;
        ALTER TABLE student_fee_adjustments     NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE student_fee_adjustments     DISABLE  ROW LEVEL SECURITY;
        ALTER TABLE student_fee_payments        NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE student_fee_payments        DISABLE  ROW LEVEL SECURITY;
        ALTER TABLE student_fee_records         NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE student_fee_records         DISABLE  ROW LEVEL SECURITY;
        """)

    # ── 1. REVOKE ─────────────────────────────────────────────────────────
    op.execute("""
        REVOKE SELECT, UPDATE ON student_growth_reports FROM ivy_parent_role;
        REVOKE SELECT ON student_measurements           FROM ivy_parent_role;
        REVOKE SELECT ON student_fee_refunds            FROM ivy_parent_role;
        REVOKE SELECT ON student_fee_adjustments        FROM ivy_parent_role;
        REVOKE SELECT ON student_fee_payments           FROM ivy_parent_role;
        REVOKE SELECT ON student_fee_records            FROM ivy_parent_role;
        """)
