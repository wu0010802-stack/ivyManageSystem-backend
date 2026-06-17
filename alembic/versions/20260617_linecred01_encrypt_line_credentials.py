"""[C41] LineConfig 憑證欄位放寬為 Text + backfill 加密

Revision ID: linecred01
Revises: audwrt02
Create Date: 2026-06-17

channel_access_token / channel_secret 改用 EncryptedText（ORM 透明 Fernet 加密）。
原欄位為明文 String(512) / String(256)：DB 外洩即直接暴露 LINE Messaging API
憑證（可冒名推播 / 偽造 webhook 簽名）。對齊 models/portfolio.py StudentAllergy
醫療欄位的加密做法（allergyenc01 先例）。

兩件事：
  1. ALTER 欄位型別 String → Text：Fernet 密文長度 >> 原 varchar 上限
     （512-char token 加密後約 780 chars、256-char secret 約 440 chars，
     塞不下原 varchar）。EncryptedText 底層 impl 即為 Text。
  2. backfill：把既有「明文」憑證透過加密器重寫為密文。is_encrypted() 判斷
     已是 Fernet token 的列則跳過（冪等，可重跑）。

⚠ 本 migration 對 dev DB 不執行（留 prod gate）；backfill 需要
MEDICAL_FIELD_ENCRYPTION_KEY 可用。SQLite / 其他 dialect 走
Base.metadata.create_all（EncryptedText 底層即 Text），不經此 migration。

downgrade：僅還原欄位型別定義為原 varchar 上限；**不解密**。若 backfill 已執行
（欄位含 Fernet 密文，長度 >> 512/256）縮回 varchar 會失敗 —— downgrade 適用於
尚未 backfill 的回退窗口（對齊 allergyenc01 docstring）。

Refs: utils/medical_field_type.EncryptedText、allergyenc01、CLAUDE.md §8/醫療加密
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "linecred01"
down_revision = "audwrt02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite / 其他 dialect：test schema 走 create_all，不限制 varchar 長度。
        return

    # 1) 放寬欄位型別（Fernet 密文 >> 原 varchar 上限）
    op.alter_column(
        "line_configs",
        "channel_access_token",
        existing_type=sa.String(length=512),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "line_configs",
        "channel_secret",
        existing_type=sa.String(length=256),
        type_=sa.Text(),
        existing_nullable=True,
    )

    # 2) backfill：既有明文憑證 → 密文（已加密的列跳過，冪等可重跑）
    from utils.medical_encryption import encrypt_medical, is_encrypted

    rows = bind.execute(
        sa.text("SELECT id, channel_access_token, channel_secret FROM line_configs")
    ).fetchall()
    for row in rows:
        cfg_id, token, secret = row
        updates = {}
        if token and not is_encrypted(token):
            updates["channel_access_token"] = encrypt_medical(token)
        if secret and not is_encrypted(secret):
            updates["channel_secret"] = encrypt_medical(secret)
        if not updates:
            continue
        set_clause = ", ".join(f"{col} = :{col}" for col in updates)
        bind.execute(
            sa.text(f"UPDATE line_configs SET {set_clause} WHERE id = :id"),
            {**updates, "id": cfg_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 僅還原型別定義；若已 backfill（欄位含超長密文）將失敗（見 docstring）。
    op.alter_column(
        "line_configs",
        "channel_secret",
        existing_type=sa.Text(),
        type_=sa.String(length=256),
        existing_nullable=True,
    )
    op.alter_column(
        "line_configs",
        "channel_access_token",
        existing_type=sa.Text(),
        type_=sa.String(length=512),
        existing_nullable=True,
    )
