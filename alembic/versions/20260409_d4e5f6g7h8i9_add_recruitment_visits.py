"""add recruitment_visits table

新增招生訪視記錄表，儲存幼兒園的招生參觀紀錄，
包含幼生資訊、來源、介紹者、預繳狀態等欄位。

Revision ID: d4e5f6g7h8i9
Revises: c3d4e5f6g7h8
Create Date: 2026-04-09 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "d4e5f6g7h8i9"
down_revision = "c3d4e5f6g7h8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "recruitment_visits" in inspector.get_table_names():
        return

    op.create_table(
        "recruitment_visits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("month", sa.String(length=10), nullable=False),
        sa.Column("seq_no", sa.String(length=10), nullable=True),
        sa.Column("visit_date", sa.String(length=50), nullable=True),
        sa.Column("child_name", sa.String(length=50), nullable=False),
        sa.Column("birthday", sa.Date(), nullable=True),
        sa.Column("grade", sa.String(length=20), nullable=True),
        sa.Column("phone", sa.String(length=100), nullable=True),
        sa.Column("address", sa.String(length=200), nullable=True),
        sa.Column("district", sa.String(length=30), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("referrer", sa.String(length=50), nullable=True),
        sa.Column("deposit_collector", sa.String(length=50), nullable=True),
        sa.Column("has_deposit", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("parent_response", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recruitment_visits_id", "recruitment_visits", ["id"], unique=False)
    op.create_index("ix_recruitment_visits_month", "recruitment_visits", ["month"], unique=False)
    op.create_index("ix_recruitment_visits_district", "recruitment_visits", ["district"], unique=False)
    op.create_index("ix_recruitment_visits_source", "recruitment_visits", ["source"], unique=False)
    op.create_index("ix_recruitment_visits_referrer", "recruitment_visits", ["referrer"], unique=False)
    op.create_index("ix_recruitment_month_grade", "recruitment_visits", ["month", "grade"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "recruitment_visits" not in inspector.get_table_names():
        return

    op.drop_index("ix_recruitment_month_grade", table_name="recruitment_visits")
    op.drop_index("ix_recruitment_visits_referrer", table_name="recruitment_visits")
    op.drop_index("ix_recruitment_visits_source", table_name="recruitment_visits")
    op.drop_index("ix_recruitment_visits_district", table_name="recruitment_visits")
    op.drop_index("ix_recruitment_visits_month", table_name="recruitment_visits")
    op.drop_index("ix_recruitment_visits_id", table_name="recruitment_visits")
    op.drop_table("recruitment_visits")
