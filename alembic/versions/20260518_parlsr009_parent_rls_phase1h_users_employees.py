"""parent_rls_phase1h_users_employees: users RLS (self-only) + employees name catalog

Revision ID: parlsr009
Revises: parlsr008
Create Date: 2026-05-18

Phase 1h — `profile.py` 切到 parent_engine 的 DB 配套（calendar.py / family.py
本身觸的表全部已在前期 RLS / catalog，不需 migration；assistant.py 無 DB
access，本期完全跳過）。

# Two tables added

## users
- ENABLE+FORCE RLS with Class B direct policy `id = current_user_id`
- GRANT SELECT on the full row (parent only sees own row by RLS).
  Includes columns like `password_hash` and `permissions` — these are fine
  because the parent has already authenticated as that user; revealing their
  own hash doesn't escalate. The RLS-enforced row scope is the key control.

## employees
- GRANT SELECT (id, name) — **column-level grant** instead of RLS.
  Teachers' names are needed by parent (班導/副班導/美術老師 display);
  other columns (`salary`, `personal_id_number`, etc.) stay parent-invisible.
  No RLS needed because parent should be able to see all teachers' names —
  filtering is a column issue, not a row issue.

# Why column-level GRANT instead of a view
A view (`parent_safe_employees`) would also work, but it adds a name to maintain
and an extra GRANT chain. Column-level GRANT is the lighter-weight idiom for
"hide sensitive columns from a role" and works automatically with any
`session.query(Employee.id, Employee.name)` SQLAlchemy expression.
"""

from __future__ import annotations

from alembic import op

revision = "parlsr009"
down_revision = "parlsr008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. users: RLS self-only + full GRANT ──────────────────────────────
    op.execute("""
        GRANT SELECT ON users TO ivy_parent_role;

        ALTER TABLE users ENABLE ROW LEVEL SECURITY;
        ALTER TABLE users FORCE  ROW LEVEL SECURITY;

        CREATE POLICY parent_isolate_users ON users
        FOR SELECT TO ivy_parent_role
        USING (
            id = NULLIF(current_setting('app.current_user_id', true), '')::int
        );
        """)

    # ── 2. employees: column-level GRANT (id, name) ───────────────────────
    op.execute("GRANT SELECT (id, name) ON employees TO ivy_parent_role")


def downgrade() -> None:
    # ── 2. employees ──────────────────────────────────────────────────────
    op.execute("REVOKE SELECT (id, name) ON employees FROM ivy_parent_role")

    # ── 1. users ──────────────────────────────────────────────────────────
    op.execute("""
        DROP POLICY IF EXISTS parent_isolate_users ON users;

        ALTER TABLE users NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE users DISABLE  ROW LEVEL SECURITY;

        REVOKE SELECT ON users FROM ivy_parent_role;
        """)
