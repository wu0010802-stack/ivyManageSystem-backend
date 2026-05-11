"""merge gov_data_sync + appraisal heads

Revision ID: f0ac312f781c
Revises: 05df4844e040, a9p0p1r2i3s4
Create Date: 2026-05-11 20:47:02.322778

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f0ac312f781c'
down_revision: Union[str, Sequence[str], None] = ('05df4844e040', 'a9p0p1r2i3s4')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
