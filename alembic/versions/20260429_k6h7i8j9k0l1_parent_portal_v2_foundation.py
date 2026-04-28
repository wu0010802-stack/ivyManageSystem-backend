"""家長入口 2.0 — Foundation: event_acknowledgments.signature_attachment_id

Phase 1 Foundation：替「事件簽收手寫簽名圖」鋪基礎欄位。
其餘 foundation 改動（Permission 位元、ATTACHMENT_OWNER_TYPES 常數、ENTITY_PATTERNS 註冊）
為 Python-only 修改，無 schema 異動。

Revision ID: k6h7i8j9k0l1
Revises: j5g6h7i8j9k0
Create Date: 2026-04-29
"""

import sqlalchemy as sa
from alembic import op

revision = "k6h7i8j9k0l1"
down_revision = "j5g6h7i8j9k0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "event_acknowledgments",
        sa.Column(
            "signature_attachment_id",
            sa.Integer(),
            nullable=True,
            comment="手寫簽名圖（PNG）；NULL 表示僅以姓名簽（向下相容舊資料）",
        ),
    )
    op.create_foreign_key(
        "fk_event_ack_signature_attachment",
        "event_acknowledgments",
        "attachments",
        ["signature_attachment_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint(
        "fk_event_ack_signature_attachment",
        "event_acknowledgments",
        type_="foreignkey",
    )
    op.drop_column("event_acknowledgments", "signature_attachment_id")
