"""gov_data_sync head shim（原為 gov_data + appraisal 雙頭 merge）

舊 appraisal 分支已隨「半年考核 + 年終獎金重構」整批砍除，本檔保留為單頭佔位
（gov_data_sync 鏈尾 → 後續 moe_phase4 / bug_sweep 都接此），方便後續鏈接，
不再做任何 schema 變更。

Revision ID: f0ac312f781c
Revises: 05df4844e040
Create Date: 2026-05-11 20:47:02.322778
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f0ac312f781c'
down_revision: Union[str, Sequence[str], None] = '05df4844e040'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
