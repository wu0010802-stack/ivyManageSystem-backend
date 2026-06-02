"""RA-MED-10 StudentAllergy 加密欄位放寬為 Text

Revision ID: allergyenc01
Revises: mergeheads11
Create Date: 2026-06-02

allergen / reaction_symptom 改用 EncryptedText（ORM 透明 Fernet 加密）。Fernet
密文長度 >> 原 varchar 上限（allergen varchar(100) / reaction_symptom varchar(200)），
塞不下 → ALTER 為 Text。first_aid_note 原已是 Text（不需 ALTER，仍套 EncryptedText）。
severity 維持 String(10) 明文（DB-level ORDER BY 排序需求，不加密）。

加密本身為 ORM-only（DB 仍存 Text 密文字串），故 migration 僅負責放寬欄位型別；
既有明文資料的加密走手動 backfill script scripts/encrypt_student_allergies.py。

downgrade：還原為原 varchar 上限。**注意**：downgrade 僅還原欄位型別定義，
不負責解密 — 若 backfill 已執行（欄位含 Fernet 密文，長度 >> 100/200），縮回
varchar 會因現有密文超長而失敗；downgrade 適用於尚未 backfill 的回退窗口。

Refs: docs/superpowers/plans/2026-06-02-security-reaudit-fix-medium.md Task 7
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "allergyenc01"
down_revision = "mergeheads11"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite / 其他 dialect 不限制 varchar 長度且 ALTER TYPE 受限；
        # test schema 走 Base.metadata.create_all 不經此 migration。
        return
    op.alter_column(
        "student_allergies",
        "allergen",
        existing_type=sa.String(length=100),
        type_=sa.Text(),
        existing_nullable=False,
    )
    op.alter_column(
        "student_allergies",
        "reaction_symptom",
        existing_type=sa.String(length=200),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 僅還原型別定義；若欄位已含 Fernet 密文（超長）將失敗（見 docstring）。
    op.alter_column(
        "student_allergies",
        "reaction_symptom",
        existing_type=sa.Text(),
        type_=sa.String(length=200),
        existing_nullable=True,
    )
    op.alter_column(
        "student_allergies",
        "allergen",
        existing_type=sa.Text(),
        type_=sa.String(length=100),
        existing_nullable=False,
    )
