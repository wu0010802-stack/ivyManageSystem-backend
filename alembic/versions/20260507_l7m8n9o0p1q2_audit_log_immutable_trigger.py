"""audit_logs 不可竄改 trigger（拒絕 UPDATE / DELETE）

Revision ID: l7m8n9o0p1q2
Revises: k6l7m8n9o0p1
Create Date: 2026-05-07

Why:
    稽核軌跡（audit_logs）必須結構性不可竄改，否則 admin（含具 DB 連線者
    走 psql）可 silent UPDATE/DELETE 改寫已記錄的操作軌跡。對齊
    student_medication_logs 的 trg_medication_log_immutable 設計。

    INSERT 不擋（這是稽核唯一進入路徑）；UPDATE / DELETE 一律 raise。

    Refs: 邏輯漏洞 audit 2026-05-07 P0 #12（user 拍板採 DB trigger 方案）。
"""

import sqlalchemy as sa
from alembic import op

revision = "l7m8n9o0p1q2"
down_revision = "k6l7m8n9o0p1"
branch_labels = None
depends_on = None


T_AUDIT = "audit_logs"


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        # PG 版：共用一個 plpgsql function 給 UPDATE / DELETE 兩個 trigger
        op.execute(sa.text("""
                CREATE OR REPLACE FUNCTION audit_log_immutable_fn()
                RETURNS trigger AS $$
                BEGIN
                    IF (TG_OP = 'UPDATE') THEN
                        RAISE EXCEPTION
                            'audit_logs 為不可竄改稽核軌跡，禁止 UPDATE (id=%)',
                            OLD.id;
                    ELSIF (TG_OP = 'DELETE') THEN
                        RAISE EXCEPTION
                            'audit_logs 為不可竄改稽核軌跡，禁止 DELETE (id=%)',
                            OLD.id;
                    END IF;
                    RETURN NULL;
                END;
                $$ LANGUAGE plpgsql;
                """))
        op.execute(sa.text("""
                CREATE TRIGGER trg_audit_log_immutable_update
                BEFORE UPDATE ON audit_logs
                FOR EACH ROW
                EXECUTE FUNCTION audit_log_immutable_fn();
                """))
        op.execute(sa.text("""
                CREATE TRIGGER trg_audit_log_immutable_delete
                BEFORE DELETE ON audit_logs
                FOR EACH ROW
                EXECUTE FUNCTION audit_log_immutable_fn();
                """))
    else:
        # SQLite 版（測試用）：分別兩個 trigger，使用 RAISE(ABORT)
        op.execute(sa.text("""
                CREATE TRIGGER trg_audit_log_immutable_update
                BEFORE UPDATE ON audit_logs
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, 'audit_logs 為不可竄改稽核軌跡，禁止 UPDATE');
                END;
                """))
        op.execute(sa.text("""
                CREATE TRIGGER trg_audit_log_immutable_delete
                BEFORE DELETE ON audit_logs
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, 'audit_logs 為不可竄改稽核軌跡，禁止 DELETE');
                END;
                """))


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_audit_log_immutable_update ON audit_logs"
            )
        )
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_audit_log_immutable_delete ON audit_logs"
            )
        )
        op.execute(sa.text("DROP FUNCTION IF EXISTS audit_log_immutable_fn()"))
    else:
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_audit_log_immutable_update"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_audit_log_immutable_delete"))
