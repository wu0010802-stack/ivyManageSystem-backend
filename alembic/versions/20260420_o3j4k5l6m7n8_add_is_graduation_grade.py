"""add is_graduation_grade to class_grades

Revision ID: o3j4k5l6m7n8
Revises: n2i3j4k5l6m7
Create Date: 2026-04-20

新增 `class_grades.is_graduation_grade` 布林欄位，用於標記「畢業班年級」，
供自動畢業排程（7/31 將該年級在讀學生轉為 graduated）判斷。

預設 False；部署後將 name='大班' 的列回填 True 以保留現有語意。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "o3j4k5l6m7n8"
down_revision = "n2i3j4k5l6m7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "class_grades" not in inspector.get_table_names():
        return

    existing_cols = {c["name"] for c in inspector.get_columns("class_grades")}
    if "is_graduation_grade" in existing_cols:
        return

    op.add_column(
        "class_grades",
        sa.Column(
            "is_graduation_grade",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="是否為畢業班年級（大班）。自動畢業排程以此為判斷依據",
        ),
    )

    # 回填：把名字為「大班」的年級標記為畢業班
    op.execute("UPDATE class_grades SET is_graduation_grade = TRUE WHERE name = '大班'")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "class_grades" not in inspector.get_table_names():
        return
    existing_cols = {c["name"] for c in inspector.get_columns("class_grades")}
    if "is_graduation_grade" in existing_cols:
        op.drop_column("class_grades", "is_graduation_grade")
