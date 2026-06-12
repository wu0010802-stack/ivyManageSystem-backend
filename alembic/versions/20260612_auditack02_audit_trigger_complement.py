"""audit_logs 不可變 trigger 改「允許清單補集」寫法（fail-open → fail-closed）

Revision ID: auditack02
Revises: cfgyrfx01
Create Date: 2026-06-12

Why（設計體檢 2026-06-12 Finding 2）:
    auditack01 的 audit_log_immutable_fn() UPDATE 分支用「逐欄列舉既有 14 欄
    不得變更」的 denylist——日後 audit_logs 加任何新欄位，該欄即可被自由竄改
    （fail-open）。本 migration CREATE OR REPLACE 同名 function，改為允許清單
    補集：`to_jsonb(NEW/OLD) 各自移除 acknowledged_at / acknowledged_by 後
    IS DISTINCT FROM` 則 RAISE——新增欄位自動受保護，無需再改 trigger。

    DELETE 分支維持 auditrelax01/auditack01 的 audit_archiver bypass 不變。

USER manual ops:
    無。Postgres-only；SQLite（測試）無此 trigger，upgrade/downgrade 皆 no-op。

Refs:
    - 前置：20260605_auditack01_audit_log_allow_ack_update.py
"""

import sqlalchemy as sa
from alembic import op

revision = "auditack02"
down_revision = "cfgyrfx01"
branch_labels = None
depends_on = None


# 補集版：UPDATE 僅允許 acknowledged_* 變動（以 jsonb 整列比對，欄位無關）。
_FN_COMPLEMENT = """
CREATE OR REPLACE FUNCTION audit_log_immutable_fn()
RETURNS trigger AS $$
BEGIN
    IF (TG_OP = 'UPDATE') THEN
        -- 允許清單補集：除 acknowledged_at / acknowledged_by 外，整列任何
        -- 欄位（含日後新增欄位）變動一律 RAISE。
        IF (to_jsonb(NEW) - 'acknowledged_at' - 'acknowledged_by'
            IS DISTINCT FROM
            to_jsonb(OLD) - 'acknowledged_at' - 'acknowledged_by'
        ) THEN
            RAISE EXCEPTION
                'audit_logs 內容不可竄改，僅允許更新 acknowledged_* (id=%)',
                OLD.id;
        END IF;
        RETURN NEW;
    ELSIF (TG_OP = 'DELETE') THEN
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
"""

# 還原版（= auditack01 _FN_ALLOW_ACK）：逐欄 denylist。
_FN_ALLOW_ACK_DENYLIST = """
CREATE OR REPLACE FUNCTION audit_log_immutable_fn()
RETURNS trigger AS $$
BEGIN
    IF (TG_OP = 'UPDATE') THEN
        -- 僅放行「除 acknowledged_* 外其餘欄位皆未變動」的 ack 動作。
        IF (NEW.id IS DISTINCT FROM OLD.id
            OR NEW.user_id IS DISTINCT FROM OLD.user_id
            OR NEW.username IS DISTINCT FROM OLD.username
            OR NEW.action IS DISTINCT FROM OLD.action
            OR NEW.entity_type IS DISTINCT FROM OLD.entity_type
            OR NEW.entity_id IS DISTINCT FROM OLD.entity_id
            OR NEW.summary IS DISTINCT FROM OLD.summary
            OR NEW.changes IS DISTINCT FROM OLD.changes
            OR NEW.ip_address IS DISTINCT FROM OLD.ip_address
            OR NEW.created_at IS DISTINCT FROM OLD.created_at
            OR NEW.user_agent_hash IS DISTINCT FROM OLD.user_agent_hash
            OR NEW.session_id IS DISTINCT FROM OLD.session_id
            OR NEW.impersonated_by IS DISTINCT FROM OLD.impersonated_by
            OR NEW.impersonated_by_name IS DISTINCT FROM OLD.impersonated_by_name
        ) THEN
            RAISE EXCEPTION
                'audit_logs 內容不可竄改，僅允許更新 acknowledged_* (id=%)',
                OLD.id;
        END IF;
        RETURN NEW;
    ELSIF (TG_OP = 'DELETE') THEN
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
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(_FN_COMPLEMENT))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(_FN_ALLOW_ACK_DENYLIST))
