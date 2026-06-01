"""merge permscope02 + pdevsetup01

Revision ID: mergeheads07
Revises: permscope02, pdevsetup01
Create Date: 2026-06-01 09:21:39.384995

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'mergeheads07'
down_revision: Union[str, Sequence[str], None] = ('permscope02', 'pdevsetup01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
