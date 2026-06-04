"""merge apxlal01 + iepappr01 heads

Revision ID: mergeheads11
Revises: apxlal01, iepappr01
Create Date: 2026-06-04 14:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "mergeheads11"
down_revision: Union[str, Sequence[str], None] = (
    "apxlal01",
    "iepappr01",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
