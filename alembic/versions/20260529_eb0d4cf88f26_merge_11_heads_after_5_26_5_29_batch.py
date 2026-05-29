"""merge 11 heads after 5/26-5/29 batch

Revision ID: eb0d4cf88f26
Revises: audwrt01, lncon01, pwdhist01, rcrgeoconsent01, schedhb01, staffrt01, auditfor01, auditrelax01, emppiired01, dsrreq01, medacc01
Create Date: 2026-05-29 09:41:13.463188

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eb0d4cf88f26'
down_revision: Union[str, Sequence[str], None] = ('audwrt01', 'lncon01', 'pwdhist01', 'rcrgeoconsent01', 'schedhb01', 'staffrt01', 'auditfor01', 'auditrelax01', 'emppiired01', 'dsrreq01', 'medacc01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
