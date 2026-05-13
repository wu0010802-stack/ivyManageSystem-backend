"""merge heads: waitlist final_reminder + growth_reports dedup + fee_templates

合三條從 g6h7i8j9k0l1 分岔出來的 head：
- 17fa49f72231 (activity-waitlist-autofill 的 final_reminder_sent_at column)
- h7i8j9k0l1m2 (security/growth_reports 的 partial unique index for dedup)
- t9u8v7w6x5y4 (tuition-mgmt 的 fee_templates 表 + record/refund extras)

三條皆 schema 異動，且在各自 worktree 已被應用到 dev DB；此 merge 純粹讓 alembic
看到單一 head，沒有 schema 變更。

Revision ID: m1m2m3m4m5m6
Revises: 17fa49f72231, h7i8j9k0l1m2, t9u8v7w6x5y4
Create Date: 2026-05-13
"""

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

revision = "m1m2m3m4m5m6"
down_revision = ("17fa49f72231", "h7i8j9k0l1m2", "t9u8v7w6x5y4")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
