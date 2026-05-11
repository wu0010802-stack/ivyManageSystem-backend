"""MOE Phase 1 schema: student/employee fields + 4 new tables

Revision ID: v8a9b0c1d2e3
Revises: x9y0z1a2b3c4
Create Date: 2026-05-11

"""

from alembic import op
import sqlalchemy as sa

revision = "v8a9b0c1d2e3"
down_revision = "x9y0z1a2b3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Student new columns ---
    with op.batch_alter_table("students") as batch:
        batch.add_column(sa.Column("id_number", sa.String(20), nullable=True))
        batch.add_column(
            sa.Column(
                "nationality", sa.String(20), nullable=True, server_default="本國"
            )
        )
        batch.add_column(sa.Column("household_address", sa.String(200), nullable=True))
        batch.add_column(
            sa.Column(
                "is_disadvantaged",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(sa.Column("low_income_status", sa.String(20), nullable=True))
        batch.add_column(sa.Column("indigenous_status", sa.String(20), nullable=True))
        batch.add_column(sa.Column("disability_type", sa.String(50), nullable=True))
        batch.add_column(sa.Column("disability_level", sa.String(10), nullable=True))
        batch.add_column(sa.Column("disability_cert_no", sa.String(50), nullable=True))
        batch.add_column(sa.Column("disability_cert_expiry", sa.Date(), nullable=True))

    # Partial unique index on id_number (PostgreSQL syntax)
    op.create_index(
        "uq_students_id_number_notnull",
        "students",
        ["id_number"],
        unique=True,
        postgresql_where=sa.text("id_number IS NOT NULL"),
    )

    # --- Employee new columns ---
    with op.batch_alter_table("employees") as batch:
        batch.add_column(sa.Column("staff_role_category", sa.String(20), nullable=True))
        batch.add_column(sa.Column("teacher_cert_no", sa.String(50), nullable=True))
        batch.add_column(sa.Column("teacher_cert_type", sa.String(20), nullable=True))

    # --- New table: student_disability_documents ---
    op.create_table(
        "student_disability_documents",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "student_id",
            sa.Integer(),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("doc_type", sa.String(20), nullable=False),
        sa.Column("file_path", sa.String(500), nullable=False),
        sa.Column("issued_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_disability_docs_student_type",
        "student_disability_documents",
        ["student_id", "doc_type"],
    )

    # --- New table: student_iep_records (shell for Phase 4) ---
    op.create_table(
        "student_iep_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "student_id",
            sa.Integer(),
            sa.ForeignKey("students.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("school_year", sa.Integer(), nullable=False),
        sa.Column("semester", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("current_status", sa.Text(), nullable=True),
        sa.Column("long_term_goals", sa.Text(), nullable=True),
        sa.Column("short_term_goals", sa.JSON(), nullable=True),
        sa.Column("mid_term_evaluation", sa.Text(), nullable=True),
        sa.Column("final_evaluation", sa.Text(), nullable=True),
        sa.Column("iep_team_members", sa.JSON(), nullable=True),
        sa.Column("meeting_dates", sa.JSON(), nullable=True),
        sa.Column(
            "created_by_employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id"),
            nullable=True,
        ),
        sa.Column(
            "approved_by_employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "student_id", "school_year", "semester", name="uq_iep_student_year_semester"
        ),
    )

    # --- New table: special_education_subsidies (shell for Phase 4) ---
    op.create_table(
        "special_education_subsidies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("subsidy_type", sa.String(30), nullable=False),
        sa.Column(
            "employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=False
        ),
        sa.Column("related_student_ids", sa.JSON(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("hours_or_rate", sa.Numeric(8, 2), nullable=True),
        sa.Column(
            "amount_requested", sa.Numeric(12, 2), nullable=False, server_default="0"
        ),
        sa.Column("amount_approved", sa.Numeric(12, 2), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("approval_doc_path", sa.String(500), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )

    # --- New table: monthly_enrollment_snapshots (shell for Phase 2) ---
    op.create_table(
        "monthly_enrollment_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column(
            "classroom_id",
            sa.Integer(),
            sa.ForeignKey("classrooms.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("age_group", sa.String(10), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("male_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("female_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column(
            "disadvantaged_count", sa.Integer(), nullable=True, server_default="0"
        ),
        sa.Column("disability_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("indigenous_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("foreign_count", sa.Integer(), nullable=True, server_default="0"),
        sa.Column(
            "expected_attendance_days", sa.Integer(), nullable=True, server_default="0"
        ),
        sa.Column(
            "actual_attendance_days", sa.Integer(), nullable=True, server_default="0"
        ),
        sa.Column("attendance_rate", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("snapshot_date", sa.Date(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=True),
        sa.Column("generated_by", sa.String(100), nullable=True),
        sa.UniqueConstraint(
            "year", "month", "classroom_id", "age_group", name="uq_monthly_snapshot_key"
        ),
    )


def downgrade() -> None:
    op.drop_table("monthly_enrollment_snapshots")
    op.drop_table("special_education_subsidies")
    op.drop_table("student_iep_records")
    op.drop_index(
        "ix_disability_docs_student_type", table_name="student_disability_documents"
    )
    op.drop_table("student_disability_documents")

    with op.batch_alter_table("employees") as batch:
        batch.drop_column("teacher_cert_type")
        batch.drop_column("teacher_cert_no")
        batch.drop_column("staff_role_category")

    op.drop_index("uq_students_id_number_notnull", table_name="students")
    with op.batch_alter_table("students") as batch:
        batch.drop_column("disability_cert_expiry")
        batch.drop_column("disability_cert_no")
        batch.drop_column("disability_level")
        batch.drop_column("disability_type")
        batch.drop_column("indigenous_status")
        batch.drop_column("low_income_status")
        batch.drop_column("is_disadvantaged")
        batch.drop_column("household_address")
        batch.drop_column("nationality")
        batch.drop_column("id_number")
