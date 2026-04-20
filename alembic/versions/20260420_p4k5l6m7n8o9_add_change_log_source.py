"""add source column to student_change_logs

Revision ID: p4k5l6m7n8o9
Revises: o3j4k5l6m7n8
Create Date: 2026-04-20

區分 change_log 的來源：
- 'lifecycle'：由 StudentLifecycleService.transition() 寫入的稽核軌跡，禁止事後編輯/刪除
- 'manual'：行政手動補登（ChangeLogEditorDialog），可編輯/刪除

舊資料無法可靠分辨，一律 backfill 為 'manual'（保留可編輯權）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "p4k5l6m7n8o9"
down_revision = "o3j4k5l6m7n8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "student_change_logs" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("student_change_logs")}
    if "source" in existing_cols:
        return

    op.add_column(
        "student_change_logs",
        sa.Column(
            "source",
            sa.String(length=20),
            nullable=False,
            server_default="manual",
            comment="異動紀錄來源：manual=手動補登；lifecycle=狀態機自動寫入",
        ),
    )
    # 舊資料保持可編輯（manual）
    op.execute("UPDATE student_change_logs SET source = 'manual' WHERE source IS NULL")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "student_change_logs" not in inspector.get_table_names():
        return
    existing_cols = {c["name"] for c in inspector.get_columns("student_change_logs")}
    if "source" in existing_cols:
        op.drop_column("student_change_logs", "source")
