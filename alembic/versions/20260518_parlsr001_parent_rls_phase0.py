"""parent_rls_phase0: 4 roles + guardians covering index（不啟用 RLS、不 GRANT）

Revision ID: parlsr001
Revises: fkidx001
Create Date: 2026-05-18

Phase 0 = 基礎設施 only. 這支 migration 跑完以後：
- 四個 role 存在於 cluster：ivy_parent_role / ivy_admin_role（NOLOGIN group roles）
  與 ivy_parent_login / ivy_admin_login（LOGIN 但**無密碼**，登入會失敗直到 ops 設）
- guardians 表上有 `ix_guardians_user_active` partial covering index，
  供 Phase 1+ 的 RLS policy subquery 高效命中。
- **沒有任何表 ENABLE ROW LEVEL SECURITY**；**沒有任何 GRANT**。所以 parent_login
  即便登入成功，也讀不到 public.* 任何表——這是有意為之，防止 Phase 0 與 Phase 1
  之間出現「GRANT 已開但 policy 未到位」的暴露窗口。

# Ops 部署 runbook（migration 完成後人工執行）
```sql
-- 1. 為 LOGIN role 設密碼（從 secret manager 或 prod env 取）
ALTER ROLE ivy_parent_login PASSWORD '<from-secret>';
ALTER ROLE ivy_admin_login PASSWORD '<from-secret>';
-- 2. 把密碼塞進 app .env 後重啟 ivy-backend
```

# Spike 教訓 (2026-05-18)
`BYPASSRLS` 屬性在 PG 不會透過 `IN ROLE` 繼承（與 LOGIN/SUPERUSER/CREATEDB 同列為
never-inherited）。所以 `ivy_admin_login` 自己必須直接掛 `BYPASSRLS`，不能依賴
從 `ivy_admin_role` group 繼承。
"""

from __future__ import annotations

from alembic import op

revision = "parlsr001"
down_revision = "fkidx001"
branch_labels = None
depends_on = None


# 角色與索引名稱集中常數（downgrade 對稱使用）
_PARENT_GROUP = "ivy_parent_role"
_PARENT_LOGIN = "ivy_parent_login"
_ADMIN_GROUP = "ivy_admin_role"
_ADMIN_LOGIN = "ivy_admin_login"
_GUARDIAN_INDEX = "ix_guardians_user_active"


def upgrade() -> None:
    # ── 1. Roles ───────────────────────────────────────────────────────────
    # 用 DO 塊 + pg_roles 檢查確保 idempotent（重跑不會炸）
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{_PARENT_GROUP}') THEN
                CREATE ROLE {_PARENT_GROUP} NOLOGIN;
            END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{_ADMIN_GROUP}') THEN
                CREATE ROLE {_ADMIN_GROUP} NOLOGIN BYPASSRLS;
            END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{_PARENT_LOGIN}') THEN
                CREATE ROLE {_PARENT_LOGIN} WITH LOGIN IN ROLE {_PARENT_GROUP};
            END IF;
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{_ADMIN_LOGIN}') THEN
                -- BYPASSRLS 必須直接掛在 LOGIN role 上，不能依賴 IN ROLE 繼承
                CREATE ROLE {_ADMIN_LOGIN} WITH LOGIN BYPASSRLS IN ROLE {_ADMIN_GROUP};
            END IF;
        END
        $$
        """)

    # ── 2. Guardians 覆蓋索引（partial，給 RLS subquery 用） ──────────────────
    # 現有 ix_guardians_user 只覆蓋 user_id，沒 deleted_at 也沒 student_id。
    # RLS policy 形如 `WHERE g.user_id = ? AND g.deleted_at IS NULL` 並回傳
    # student_id，這支 partial covering 直接命中。
    op.execute(f"""
        CREATE INDEX IF NOT EXISTS {_GUARDIAN_INDEX}
        ON guardians (user_id, student_id)
        WHERE deleted_at IS NULL
        """)


def downgrade() -> None:
    # ── 2. Drop covering index ─────────────────────────────────────────────
    op.execute(f"DROP INDEX IF EXISTS {_GUARDIAN_INDEX}")

    # ── 1. Drop roles ──────────────────────────────────────────────────────
    # 順序：先 DROP LOGIN roles（IN ROLE 依賴），再 DROP group roles。
    # REASSIGN OWNED 把任何持有的物件擁有權交回 ivy_owner_role（migration 跑者本身）
    # ，免得 DROP ROLE 因 dependency 失敗。
    op.execute(f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{_PARENT_LOGIN}') THEN
                EXECUTE format('REASSIGN OWNED BY %I TO CURRENT_USER', '{_PARENT_LOGIN}');
                EXECUTE format('DROP OWNED BY %I', '{_PARENT_LOGIN}');
                DROP ROLE {_PARENT_LOGIN};
            END IF;
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{_ADMIN_LOGIN}') THEN
                EXECUTE format('REASSIGN OWNED BY %I TO CURRENT_USER', '{_ADMIN_LOGIN}');
                EXECUTE format('DROP OWNED BY %I', '{_ADMIN_LOGIN}');
                DROP ROLE {_ADMIN_LOGIN};
            END IF;
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{_PARENT_GROUP}') THEN
                EXECUTE format('REASSIGN OWNED BY %I TO CURRENT_USER', '{_PARENT_GROUP}');
                EXECUTE format('DROP OWNED BY %I', '{_PARENT_GROUP}');
                DROP ROLE {_PARENT_GROUP};
            END IF;
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = '{_ADMIN_GROUP}') THEN
                EXECUTE format('REASSIGN OWNED BY %I TO CURRENT_USER', '{_ADMIN_GROUP}');
                EXECUTE format('DROP OWNED BY %I', '{_ADMIN_GROUP}');
                DROP ROLE {_ADMIN_GROUP};
            END IF;
        END
        $$
        """)
