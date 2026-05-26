"""merge audrsk01 + mergeheads03

Revision ID: mergeheads04
Revises: audrsk01, mergeheads03
Create Date: 2026-05-26

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "mergeheads04"
down_revision: Union[str, Sequence[str], None] = ("audrsk01", "mergeheads03")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
