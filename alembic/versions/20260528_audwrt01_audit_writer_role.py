"""audit_writer role + REVOKE UPDATE/DELETE / GRANT INSERT on audit_logs

Revision ID: audwrt01
Revises: intghealth01
Create Date: 2026-05-28

Why:
    Spec D defense-in-depth：trigger 已防 UPDATE/DELETE，本 migration 加：
    1. ivy_audit_writer LOGIN role（密碼由 ops 另設）
    2. REVOKE UPDATE, DELETE ON audit_logs FROM ivy_admin_role, ivy_parent_role, public
    3. GRANT INSERT ON audit_logs TO ivy_audit_writer, ivy_admin_role
    4. GRANT SELECT ON audit_logs TO ivy_admin_role
    5. GRANT USAGE, SELECT ON SEQUENCE audit_logs_id_seq TO 兩個 role
    6. CRITICAL: GRANT ivy_audit_writer TO ivy_admin_login (SET LOCAL ROLE prerequisite)

    即使 trigger 被 DROP, REVOKE 仍擋；即使 user 加 GRANT UPDATE 給 admin, trigger 仍擋。

    Refs: audit P1 #10、spec docs/superpowers/specs/2026-05-28-audit-logs-db-append-only-design.md
"""

from alembic import op
import sqlalchemy as sa

revision = "audwrt01"
down_revision = "intghealth01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ivy_audit_writer') THEN
                CREATE ROLE ivy_audit_writer WITH LOGIN;
            END IF;
        END
        $$;
    """))

    op.execute(sa.text("REVOKE UPDATE, DELETE ON audit_logs FROM PUBLIC"))
    op.execute(sa.text("REVOKE UPDATE, DELETE ON audit_logs FROM ivy_admin_role"))
    op.execute(sa.text("REVOKE UPDATE, DELETE ON audit_logs FROM ivy_parent_role"))

    op.execute(sa.text("GRANT INSERT ON audit_logs TO ivy_audit_writer"))
    op.execute(sa.text("GRANT INSERT ON audit_logs TO ivy_admin_role"))
    op.execute(sa.text("GRANT SELECT ON audit_logs TO ivy_admin_role"))

    op.execute(sa.text("GRANT USAGE, SELECT ON SEQUENCE audit_logs_id_seq TO ivy_audit_writer"))
    op.execute(sa.text("GRANT USAGE, SELECT ON SEQUENCE audit_logs_id_seq TO ivy_admin_role"))

    # CRITICAL: SET LOCAL ROLE 需要 caller 是 target role 的 member
    # 沒這個 GRANT，runtime SET LOCAL ROLE ivy_audit_writer 會 permission denied
    op.execute(sa.text("GRANT ivy_audit_writer TO ivy_admin_login"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text("REVOKE ivy_audit_writer FROM ivy_admin_login"))
    op.execute(sa.text("REVOKE INSERT ON audit_logs FROM ivy_audit_writer"))
    op.execute(sa.text("REVOKE USAGE, SELECT ON SEQUENCE audit_logs_id_seq FROM ivy_audit_writer"))
    op.execute(sa.text("GRANT UPDATE, DELETE ON audit_logs TO ivy_admin_role"))
    op.execute(sa.text("DROP ROLE IF EXISTS ivy_audit_writer"))
