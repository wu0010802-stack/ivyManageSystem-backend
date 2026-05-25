"""merge permtxt01 + mergeheads01

Revision ID: mergeheads02
Revises: permtxt01, mergeheads01
Create Date: 2026-05-25 10:29:18.661038

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'mergeheads02'
down_revision: Union[str, Sequence[str], None] = ('permtxt01', 'mergeheads01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
