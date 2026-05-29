"""audit_logs immutable trigger 加 audit_archiver role bypass

Revision ID: auditrelax01
Revises: emppiired01
Create Date: 2026-05-29

Why:
    原 trg_audit_log_immutable_delete trigger 完全擋 DELETE，導致 audit_logs
    永遠不能刪 → 違反個資法 §11「特定目的消失應主動刪除」。
    (audit changes 明文存 PII：身分證 / 銀行帳號 before/after — 第四輪 P0 #4)

    本 migration 將 trigger 改為「擋一般 user 但放行 audit_archiver Postgres role」，
    讓未來的 cold storage 匯出 + DELETE 流程（spec
    docs/superpowers/specs/2026-05-29-audit-log-cold-storage-design.md）有合規執行路徑。

    Trigger 仍擋 UPDATE 100%（稽核軌跡內容不可改）。

USER manual ops:
    upgrade 後需手動建 Postgres role：
      CREATE ROLE audit_archiver NOLOGIN;
      GRANT DELETE ON audit_logs TO audit_archiver;
    （日後 cold storage script 用 SET ROLE audit_archiver; ... RESET ROLE; pattern）

Refs:
    - 第五輪 P0 audit #1（trigger 阻塞合規修補）
    - cold storage spec: docs/superpowers/specs/2026-05-29-audit-log-cold-storage-design.md
"""

import sqlalchemy as sa
from alembic import op

revision = "auditrelax01"
down_revision = "intghealth01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        # 重新定義 plpgsql function：UPDATE 仍硬擋，DELETE 對 audit_archiver bypass
        op.execute(sa.text("""
                CREATE OR REPLACE FUNCTION audit_log_immutable_fn()
                RETURNS trigger AS $$
                BEGIN
                    IF (TG_OP = 'UPDATE') THEN
                        RAISE EXCEPTION
                            'audit_logs 為不可竄改稽核軌跡，禁止 UPDATE (id=%)',
                            OLD.id;
                    ELSIF (TG_OP = 'DELETE') THEN
                        -- 放行：current_user / session_user = audit_archiver (cold storage GC 流程)
                        IF current_user = 'audit_archiver' OR session_user = 'audit_archiver' THEN
                            RETURN OLD;
                        END IF;
                        RAISE EXCEPTION
                            'audit_logs DELETE 僅允許 audit_archiver role；現 user=% session=%',
                            current_user, session_user;
                    END IF;
                    RETURN NULL;
                END;
                $$ LANGUAGE plpgsql;
                """))
    else:
        # SQLite (測試)：保留原 ABORT 行為（cold storage 流程 Postgres-only）
        # 無 user role 概念，保留嚴格擋 DELETE 邏輯
        pass


def downgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        # 還原為完全擋 DELETE
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
