"""add is_hospitalized to leave_records

Revision ID: t8o9p0q1r2s3
Revises: s7n8o9p0q1r2
Create Date: 2026-04-22

Why:
  勞工請假規則第 4 條病假分流：未住院年 30 天、住院年 1 年。
  系統原僅有 sick 單一配額（240h），員工住院超過 30 天會被擋 → 短給法定請假權利。
  新增 is_hospitalized 欄位，配合 api/leaves_quota.py::assert_sick_leave_within_statutory_caps
  做雙配額驗證。

歷史資料：
  既有紀錄全部視為「未住院」（False），保留原行為。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "t8o9p0q1r2s3"
down_revision = "s7n8o9p0q1r2"
branch_labels = None
depends_on = None


_TABLE = "leave_records"
_COLUMN = "is_hospitalized"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in cols:
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN,
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
                comment="病假是否為住院（影響年度配額計算）",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    cols = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in cols:
        op.drop_column(_TABLE, _COLUMN)
