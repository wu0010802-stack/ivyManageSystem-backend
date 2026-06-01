"""merge portalimp01 + permscope04

Revision ID: mergeheads08
Revises: permscope04, portalimp01
Create Date: 2026-06-01 11:40:45.478584

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'mergeheads08'
down_revision: Union[str, Sequence[str], None] = ('permscope04', 'portalimp01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
