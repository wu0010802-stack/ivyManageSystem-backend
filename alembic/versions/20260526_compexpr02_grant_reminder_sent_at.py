"""加 overtime_comp_leave_grants.reminder_sent_at

Revision ID: compexpr02
Revises: mergeheads05
Create Date: 2026-05-26 10:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "compexpr02"
down_revision = "mergeheads05"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "overtime_comp_leave_grants",
        sa.Column(
            "reminder_sent_at",
            sa.DateTime,
            nullable=True,
            comment="LINE 推播提醒已發送時間（防重複）",
        ),
    )


def downgrade():
    op.drop_column("overtime_comp_leave_grants", "reminder_sent_at")
