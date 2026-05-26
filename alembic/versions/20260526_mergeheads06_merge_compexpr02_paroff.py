"""merge compexpr02 paroff01

Revision ID: mergeheads06
Revises: compexpr02, paroff01
Create Date: 2026-05-26 22:17:12.451631

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'mergeheads06'
down_revision: Union[str, Sequence[str], None] = ('compexpr02', 'paroff01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
