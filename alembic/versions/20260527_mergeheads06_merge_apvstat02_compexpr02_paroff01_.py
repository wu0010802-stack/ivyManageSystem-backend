"""merge apvstat02 + mergeheads05a

Revision ID: mergeheads06
Revises: apvstat02, mergeheads05a
Create Date: 2026-05-27

Pairs with chore/fix-ci-gates-2026-05-27 (mergeheads05a collapses
compexpr02 + paroff01 first). Once that chore PR lands, mergeheads06
just needs to collapse the remaining apvstat02 head with mergeheads05a.
"""

revision = "mergeheads06"
down_revision = ("apvstat02", "mergeheads05a")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
