"""medical_access_log: 新增 access_type 區分被動顯示 / 具理由取用

Revision ID: medacctype01
Revises: audwrt02
Create Date: 2026-06-17

Why:
    §6 特種個資取用稽核（medical_access_log）目前 reason 是自由文字：詳細頁 / 用藥單 /
    過敏清單 / 家長端「被動回出醫療欄位」寫死 generic reason（以「（無顯式理由）」結尾），
    reason-gated /medical 端點則是使用者填的 ≥10 字理由。兩者語意不同但只能靠 reason
    字串比對區分（脆弱）。

    新增結構化 access_type（passive / explicit），讓未來做 §6 取用檢視 / 匯出時能可靠
    篩選「具理由的刻意取用」與「附帶顯示」，且完全不縮減記錄覆蓋。

    新欄 server_default='passive'（多數寫入點皆被動）；歷史列依 reason 是否以
    「（無顯式理由）」結尾回填 explicit。
"""

from alembic import op
import sqlalchemy as sa

revision = "medacctype01"
down_revision = "audwrt02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "medical_access_log",
        sa.Column(
            "access_type",
            sa.String(length=20),
            nullable=False,
            server_default="passive",
        ),
    )
    # 歷史列回填：reason 不以「（無顯式理由）」結尾者 = reason-gated /medical 具理由取用。
    # 用 bindparams 帶 LIKE pattern（含 %），避免 % 進 SQL 字串被 driver 誤判為格式化佔位。
    op.execute(
        sa.text(
            "UPDATE medical_access_log SET access_type = 'explicit' "
            "WHERE reason NOT LIKE :pat"
        ).bindparams(pat="%（無顯式理由）")
    )


def downgrade() -> None:
    op.drop_column("medical_access_log", "access_type")
