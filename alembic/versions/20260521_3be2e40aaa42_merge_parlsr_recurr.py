"""merge parlsr+recurr

Revision ID: 3be2e40aaa42
Revises: parlsr011, recurr01
Create Date: 2026-05-21 08:36:07.999729

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3be2e40aaa42'
down_revision: Union[str, Sequence[str], None] = ('parlsr011', 'recurr01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
