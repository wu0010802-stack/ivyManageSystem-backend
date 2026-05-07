"""gov_data_sync

新表：
- gov_data_snapshots
- insurance_brackets_staging
- minimum_wage_history
- minimum_wage_staging

既有表異動：
- insurance_brackets 加 source_snapshot_id (nullable FK)

Bootstrap：
- minimum_wage_history 落地 2025/2026 兩筆（搬自 services/salary/minimum_wage.py 常數）

Revision ID: 05df4844e040
Revises: l7m8n9o0p1q2
Create Date: 2026-05-07 15:03:16.322674

"""

from typing import Sequence, Union
from datetime import date

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "05df4844e040"
down_revision: Union[str, Sequence[str], None] = "l7m8n9o0p1q2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. gov_data_snapshots
    op.create_table(
        "gov_data_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("source_url", sa.String(500), nullable=False),
        sa.Column(
            "fetched_at", sa.DateTime, server_default=sa.func.now(), nullable=False
        ),
        sa.Column("http_status", sa.Integer, nullable=False),
        sa.Column("raw_payload", sa.JSON, nullable=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("error", sa.Text, nullable=True),
    )
    op.create_index(
        "ix_gov_snapshot_source_time", "gov_data_snapshots", ["source", "fetched_at"]
    )
    op.create_index(
        "ix_gov_snapshot_payload_hash", "gov_data_snapshots", ["payload_hash"]
    )

    # 2. insurance_brackets_staging
    op.create_table(
        "insurance_brackets_staging",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("effective_year", sa.Integer, nullable=False),
        sa.Column(
            "composed_at", sa.DateTime, server_default=sa.func.now(), nullable=False
        ),
        sa.Column("composed_from", sa.JSON, nullable=False),
        sa.Column("brackets", sa.JSON, nullable=False),
        sa.Column("rates", sa.JSON, nullable=False),
        sa.Column("diff_summary", sa.JSON, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.String(50), nullable=True),
        sa.Column("decided_at", sa.DateTime, nullable=True),
        sa.Column("decision_reason", sa.String(500), nullable=True),
    )
    op.create_index(
        "ix_staging_year_status",
        "insurance_brackets_staging",
        ["effective_year", "status"],
    )

    # 3. minimum_wage_history
    op.create_table(
        "minimum_wage_history",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("monthly", sa.Integer, nullable=False),
        sa.Column("hourly", sa.Integer, nullable=False),
        sa.Column(
            "source_snapshot_id",
            sa.Integer,
            sa.ForeignKey("gov_data_snapshots.id"),
            nullable=True,
        ),
        sa.Column("confirmed_by", sa.String(50), nullable=False),
        sa.Column(
            "confirmed_at", sa.DateTime, server_default=sa.func.now(), nullable=False
        ),
        sa.Column("confirm_reason", sa.String(500), nullable=False),
        sa.UniqueConstraint("effective_date", name="uq_minimum_wage_effective_date"),
    )

    # 4. minimum_wage_staging
    op.create_table(
        "minimum_wage_staging",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("monthly", sa.Integer, nullable=False),
        sa.Column("hourly", sa.Integer, nullable=False),
        sa.Column(
            "source_snapshot_id",
            sa.Integer,
            sa.ForeignKey("gov_data_snapshots.id"),
            nullable=False,
        ),
        sa.Column(
            "composed_at", sa.DateTime, server_default=sa.func.now(), nullable=False
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.String(50), nullable=True),
        sa.Column("decided_at", sa.DateTime, nullable=True),
        sa.Column("decision_reason", sa.String(500), nullable=True),
    )

    # 5. insurance_brackets 加欄位
    op.add_column(
        "insurance_brackets",
        sa.Column(
            "source_snapshot_id",
            sa.Integer,
            sa.ForeignKey("gov_data_snapshots.id"),
            nullable=True,
        ),
    )

    # 6. Bootstrap minimum_wage_history
    op.bulk_insert(
        sa.table(
            "minimum_wage_history",
            sa.column("effective_date", sa.Date),
            sa.column("monthly", sa.Integer),
            sa.column("hourly", sa.Integer),
            sa.column("confirmed_by", sa.String),
            sa.column("confirm_reason", sa.String),
        ),
        [
            {
                "effective_date": date(2025, 1, 1),
                "monthly": 28590,
                "hourly": 190,
                "confirmed_by": "system",
                "confirm_reason": "初始 bootstrap，遷移自 minimum_wage.py 常數",
            },
            {
                "effective_date": date(2026, 1, 1),
                "monthly": 29500,
                "hourly": 196,
                "confirmed_by": "system",
                "confirm_reason": "初始 bootstrap，遷移自 minimum_wage.py 常數",
            },
        ],
    )


def downgrade() -> None:
    op.drop_column("insurance_brackets", "source_snapshot_id")
    op.drop_table("minimum_wage_staging")
    op.drop_table("minimum_wage_history")
    op.drop_index("ix_staging_year_status", table_name="insurance_brackets_staging")
    op.drop_table("insurance_brackets_staging")
    op.drop_index("ix_gov_snapshot_payload_hash", table_name="gov_data_snapshots")
    op.drop_index("ix_gov_snapshot_source_time", table_name="gov_data_snapshots")
    op.drop_table("gov_data_snapshots")
