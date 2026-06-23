"""activity_courses 新增 instructor_name（講師姓名，前台 advisory 顯示）

Revision ID: actvinstr01
Revises: actvcnt01
Create Date: 2026-06-23

才藝課程卡新增「講師」顯示（review finding #2）。才藝老師常為外聘、非園內員工，
故用自由字串 instructor_name（String(50), nullable）而非 employee FK；日後若要接
員工/薪資再升級為關聯欄。nullable 無預設，既有列為 NULL（前端缺值不顯示）。

downgrade：drop_column instructor_name。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "actvinstr01"
down_revision = "actvcnt01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "activity_courses",
        sa.Column("instructor_name", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("activity_courses", "instructor_name")
