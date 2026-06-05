"""org_year_settings 加 school_achievement_rate_override（HR 手動覆寫全校達成率）

NULL=用自算 school_achievement_rate。純 nullable add column，零回填、可逆。

Revision ID: yeschr01
Revises: cfgyear01
Create Date: 2026-06-06
"""

from alembic import op
import sqlalchemy as sa

revision = "yeschr01"
down_revision = "cfgyear01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "org_year_settings",
        sa.Column(
            "school_achievement_rate_override",
            sa.Numeric(6, 3),
            nullable=True,
            comment="HR 手動覆寫全校達成率；NULL=用自算 school_achievement_rate",
        ),
    )


def downgrade():
    op.drop_column("org_year_settings", "school_achievement_rate_override")
