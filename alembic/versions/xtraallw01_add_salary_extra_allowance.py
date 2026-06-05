"""add salary extra_allowance + extra_allowance_label (records + snapshots)

額外加給（值週/活動加班費等，手填）金額 + 名目；同步加到 salary_records 與
salary_snapshots 兩表（snapshot service 依兩表欄位交集反射複製，漏欄則快照遺失）。

Revision ID: xtraallw01
Revises: latededu01
Create Date: 2026-06-04
"""

from alembic import op
import sqlalchemy as sa

revision = "xtraallw01"
down_revision = "latededu01"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("salary_records", "salary_snapshots"):
        op.add_column(
            table,
            sa.Column(
                "extra_allowance",
                sa.Numeric(12, 2),
                nullable=False,
                server_default="0",
            ),
        )
        op.add_column(
            table,
            sa.Column("extra_allowance_label", sa.String(length=50), nullable=True),
        )


def downgrade():
    for table in ("salary_records", "salary_snapshots"):
        op.drop_column(table, "extra_allowance_label")
        op.drop_column(table, "extra_allowance")
