"""merge heads after mass-merge (acadhk02 ayebsr1 pretent001 empleavesync)

Revision ID: mergeheads01
Revises: acadhk02, ayebsr1, pretent001, empleavesync
Create Date: 2026-05-25 09:49:27.587377

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'mergeheads01'
down_revision: Union[str, Sequence[str], None] = ('acadhk02', 'ayebsr1', 'pretent001', 'empleavesync')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
