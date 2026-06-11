"""audit_logs immutable trigger 放寬：允許僅更新 acknowledged_* 的 ack 動作

Revision ID: auditack01
Revises: yebnd01
Create Date: 2026-06-05

Why（Finding G）:
    audit_log_immutable_fn() 的 UPDATE 分支 100% RAISE（auditrelax01），但高風險
    事件 ack 端點（api/audit.py ack_audit / ack_all_audits）對 audit_logs 執行
    UPDATE acknowledged_at / acknowledged_by → 在 PostgreSQL 上一律被 trigger 擋掉，
    紅點告警 ack 功能在 prod 完全失效。測試用 Base.metadata.create_all 不套此
    trigger，故 SQLite 過綠但 prod 路徑從未被測到。

    本 migration 放寬 UPDATE 分支：僅當「除 acknowledged_at / acknowledged_by 外
    所有欄位皆未變動」時放行（RETURN NEW），其餘任何欄位變動仍 RAISE。稽核軌跡
    內容（user/action/entity/summary/changes/created_at/impersonation 等）維持
    不可竄改。DELETE 分支維持 auditrelax01 的 audit_archiver bypass 不變。

USER manual ops:
    無新增 role 需求（沿用 auditrelax01 的 audit_archiver）。Postgres-only；
    SQLite（測試）無此 trigger，upgrade/downgrade 皆 no-op。

Refs:
    - Finding G（第二輪「繼續幫我找」授權/PII 面）
    - 前置 trigger：20260529_auditrelax01_audit_log_archiver_bypass.py
"""

import sqlalchemy as sa
from alembic import op

revision = "auditack01"
down_revision = "yebnd01"
branch_labels = None
depends_on = None


# 放寬版：UPDATE 僅允許 acknowledged_* 變動；DELETE 維持 audit_archiver bypass。
_FN_ALLOW_ACK = """
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

# 還原版（= auditrelax01）：UPDATE 100% 擋；DELETE 維持 audit_archiver bypass。
_FN_BLOCK_UPDATE = """
CREATE OR REPLACE FUNCTION audit_log_immutable_fn()
RETURNS trigger AS $$
BEGIN
    IF (TG_OP = 'UPDATE') THEN
        RAISE EXCEPTION
            'audit_logs 為不可竄改稽核軌跡，禁止 UPDATE (id=%)',
            OLD.id;
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
        op.execute(sa.text(_FN_ALLOW_ACK))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text(_FN_BLOCK_UPDATE))
