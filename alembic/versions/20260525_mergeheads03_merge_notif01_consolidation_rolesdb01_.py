"""merge notif01_consolidation + rolesdb01 + offb0001

Revision ID: mergeheads03
Revises: notif01_consolidation, rolesdb01, offb0001
Create Date: 2026-05-25 17:33:15.254487

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'mergeheads03'
down_revision: Union[str, Sequence[str], None] = ('notif01_consolidation', 'rolesdb01', 'offb0001')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
