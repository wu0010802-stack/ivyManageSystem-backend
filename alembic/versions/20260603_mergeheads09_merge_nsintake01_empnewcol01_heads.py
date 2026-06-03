"""merge nsintake01 + empnewcol01 heads

Revision ID: mergeheads09
Revises: empnewcol01, nsintake01
Create Date: 2026-06-03 16:28:30.600953

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'mergeheads09'
down_revision: Union[str, Sequence[str], None] = ('empnewcol01', 'nsintake01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
