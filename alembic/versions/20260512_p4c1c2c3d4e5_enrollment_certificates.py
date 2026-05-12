"""enrollment_certificates table for MOE phase 4 sub-system C

Revision ID: p4c1c2c3d4e5
Revises: f0ac312f781c
Create Date: 2026-05-12
"""

from alembic import op
import sqlalchemy as sa

revision = "p4c1c2c3d4e5"
down_revision = "f0ac312f781c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "enrollment_certificates",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "student_id",
            sa.Integer,
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("purpose", sa.String(200), nullable=False),
        sa.Column("copies", sa.Integer, nullable=False, server_default="1"),
        sa.Column("issue_date", sa.Date, nullable=False),
        sa.Column(
            "issued_by_user_id",
            sa.Integer,
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("pdf_path", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("year", "seq", name="uq_enrollment_cert_year_seq"),
    )
    op.create_index(
        "ix_enrollment_cert_student",
        "enrollment_certificates",
        ["student_id"],
    )
    op.create_index(
        "ix_enrollment_cert_year",
        "enrollment_certificates",
        ["year"],
    )


def downgrade() -> None:
    op.drop_index("ix_enrollment_cert_year", table_name="enrollment_certificates")
    op.drop_index("ix_enrollment_cert_student", table_name="enrollment_certificates")
    op.drop_table("enrollment_certificates")
