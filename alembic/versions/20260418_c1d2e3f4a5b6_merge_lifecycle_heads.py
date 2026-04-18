"""merge heads before student lifecycle refactor

合併兩個並行的 migration heads：
- `s6t7u8v9w0x1` (2026-03-20 add_probation_end_date)
- `b0c1d2e3f4a5` (2026-04-18 add_activity_pos_daily_close)

學生生命週期追蹤 (Phase A) 的 migration 將基於此 merge 節點。

Revision ID: c1d2e3f4a5b6
Revises: s6t7u8v9w0x1, b0c1d2e3f4a5
Create Date: 2026-04-18
"""

from alembic import op  # noqa: F401

revision = "c1d2e3f4a5b6"
down_revision = ("s6t7u8v9w0x1", "b0c1d2e3f4a5")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
