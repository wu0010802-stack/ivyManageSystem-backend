"""appraisal_summary_log: 簽核軌跡表（Phase 2 signing UX）

Revision ID: aprsig001
Revises: aprcal001
Create Date: 2026-05-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "aprsig001"
down_revision = "aprcal001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Task 2 補實作
    pass


def downgrade() -> None:
    # Task 2 補實作
    pass
