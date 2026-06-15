"""audit_writer: GRANT SELECT (id) on audit_logs（補 INSERT ... RETURNING id）

Revision ID: audwrt02
Revises: enrdwt01
Create Date: 2026-06-15

Why:
    audwrt01 只 GRANT INSERT ON audit_logs TO ivy_audit_writer，沒給 SELECT。
    但 ORM 寫入（utils.audit._write_audit_sync → session.add(AuditLog); commit）
    在 PG 上產生的是 `INSERT INTO audit_logs (...) RETURNING audit_logs.id`，
    RETURNING 子句需要對 id 欄的 SELECT 權限。

    結果：背景稽核寫入路徑（登入成功/失敗事件、middleware 等走 _write_audit_sync
    而非 write_audit_in_session 的 path）在 `SET LOCAL ROLE ivy_audit_writer`
    成功後，會在 RETURNING id 上 permission denied → 被 fail-open 默默丟掉
    （只留一條 "Audit log write failed" warning）→ 稽核軌跡缺漏。

    補 **column-level** SELECT(id)（而非整表 SELECT）：剛好滿足 RETURNING id，
    同時保留 audwrt01「ivy_audit_writer 只能寫、不能讀稽核 PII（changes/summary
    等）」的 defense-in-depth 設計意圖。

    冪等：PG 對重複 GRANT 不報錯；dev 已先手動套用同一 grant，本 migration 正規化。

    Refs: audwrt01（audit_writer role）、utils/audit.py:_write_audit_sync
"""

from alembic import op
import sqlalchemy as sa

revision = "audwrt02"
down_revision = "enrdwt01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # column-level：只放行 RETURNING id 需要的 id 欄 SELECT，不暴露稽核 PII 欄。
    # role-existence guard（對齊 audwrt01:38）：正常鏈 audwrt01 必先建好 role，
    # 此處 guard 純防手動跳 stamp / 漂移時整支 migration 直接炸。
    op.execute(sa.text("""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'ivy_audit_writer') THEN
                GRANT SELECT (id) ON audit_logs TO ivy_audit_writer;
            END IF;
        END
        $$;
    """))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # guard：role 已被先一步 drop（手動 / 部分 downgrade）時 REVOKE 不致中止 downgrade。
    op.execute(sa.text("""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'ivy_audit_writer') THEN
                REVOKE SELECT (id) ON audit_logs FROM ivy_audit_writer;
            END IF;
        END
        $$;
    """))
