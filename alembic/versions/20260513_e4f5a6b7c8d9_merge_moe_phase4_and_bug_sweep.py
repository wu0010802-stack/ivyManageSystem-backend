"""merge heads: bug_sweep_2026-05-12 + moe_phase4_enrollment_certificates

MOE Phase 4 (p4c1c2c3d4e5) 與 bug sweep 鏈 (c2d3e4f5a6b7) 都從 f0ac312f781c
分岔出去並各自應用到 dev DB / 主 chain。此 merge migration 把兩條 head 合併
回單一鏈，方便後續 migration (例如成長檔案 P1 的 d3e4f5a6b7c8) 接續。

純 merge migration：沒有 schema 變更，只是讓 alembic 看到單一 head。

Revision ID: e4f5a6b7c8d9
Revises: c2d3e4f5a6b7, p4c1c2c3d4e5
Create Date: 2026-05-13
"""

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401

revision = "e4f5a6b7c8d9"
down_revision = ("c2d3e4f5a6b7", "p4c1c2c3d4e5")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
