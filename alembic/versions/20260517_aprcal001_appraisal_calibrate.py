"""appraisal_calibrate: scoring rules + manual event counts

Revision ID: aprcal001
Revises: f33ty9types, r4c3c0nd5n4p
Create Date: 2026-05-17
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "aprcal001"
down_revision: Union[str, Sequence[str], None] = ("f33ty9types", "r4c3c0nd5n4p")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Task 4 補實作
    pass


def downgrade() -> None:
    # Task 4 補實作
    pass
