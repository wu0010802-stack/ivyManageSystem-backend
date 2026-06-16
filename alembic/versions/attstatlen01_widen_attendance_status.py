"""bh-attendance #2 放寬 attendances.status 為 String(40)

Revision ID: attstatlen01
Revises: enrdwt01
Create Date: 2026-06-16

考勤 status 為「開放複合值域」：utils/attendance_calc.py、services/attendance_parser.py、
api/attendance/upload.py 以 '+' 串接多個旗標，最長如
'late+early_leave+missing_punch_out'（34 字），超過舊 `attendances.status` VARCHAR(20)
上限。在 PostgreSQL 上寫入即 `value too long for type character varying(20)`（DataError），
複合考勤（同時遲到 + 早退 + 缺打卡）整批匯入失敗。

本 migration 把欄位放寬為 String(40)，足以容納最長複合值並留緩衝。

downgrade：還原為 String(20)。**注意**：若已有列存入超過 20 字的複合 status，
縮回 VARCHAR(20) 會因現有值超長而失敗（PostgreSQL）；downgrade 適用於尚無超長值
的回退窗口。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "attstatlen01"
down_revision = "vpamt01"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite / 其他 dialect 不限制 varchar 長度；測試 schema 走
        # Base.metadata.create_all 不經此 migration。
        return
    op.alter_column(
        "attendances",
        "status",
        existing_type=sa.String(length=20),
        type_=sa.String(length=40),
        existing_nullable=False,
    )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 還原為 String(20)；若現有列含 >20 字複合 status 將失敗（見 docstring，可能截斷）。
    op.alter_column(
        "attendances",
        "status",
        existing_type=sa.String(length=40),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
