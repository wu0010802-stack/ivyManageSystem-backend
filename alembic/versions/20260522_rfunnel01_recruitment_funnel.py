"""recruitment funnel phase a: academic_terms + recruitment_event_log

Revision ID: rfunnel01
Revises: 3be2e40aaa42
Create Date: 2026-05-22
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "rfunnel01"
down_revision: Union[str, Sequence[str], None] = "3be2e40aaa42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "academic_terms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("school_year", sa.Integer(), nullable=False),
        sa.Column("semester", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "school_year", "semester", name="uq_academic_terms_year_semester"
        ),
        sa.CheckConstraint(
            "end_date > start_date", name="ck_academic_terms_date_order"
        ),
        sa.CheckConstraint(
            "semester IN (1, 2)", name="ck_academic_terms_semester_valid"
        ),
    )

    op.create_table(
        "recruitment_event_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "recruitment_visit_id",
            sa.Integer(),
            sa.ForeignKey("recruitment_visits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("from_stage", sa.String(length=20), nullable=True),
        sa.Column("to_stage", sa.String(length=20), nullable=False),
        sa.Column(
            "student_id",
            sa.Integer(),
            sa.ForeignKey("students.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_recruitment_event_log_visit_time",
        "recruitment_event_log",
        ["recruitment_visit_id", "created_at"],
    )
    op.create_index(
        "ix_recruitment_event_log_event_type", "recruitment_event_log", ["event_type"]
    )
    op.create_index(
        "ix_recruitment_event_log_actor", "recruitment_event_log", ["actor_user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_recruitment_event_log_actor", table_name="recruitment_event_log")
    op.drop_index(
        "ix_recruitment_event_log_event_type", table_name="recruitment_event_log"
    )
    op.drop_index(
        "ix_recruitment_event_log_visit_time", table_name="recruitment_event_log"
    )
    op.drop_table("recruitment_event_log")
    op.drop_table("academic_terms")
