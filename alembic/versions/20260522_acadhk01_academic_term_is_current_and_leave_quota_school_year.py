"""academic_term is_current and leave_quota school_year

Revision ID: acadhk01
Revises: rfunnel01
Create Date: 2026-05-22

Schema 增量，無 data migration。
- academic_terms.is_current: 目前學期 flag，partial unique singleton
- leave_quotas.school_year: 民國學年；nullable（共存 legacy year-based row）
"""

from alembic import op
import sqlalchemy as sa

revision = "acadhk01"
down_revision = "rfunnel01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # === AcademicTerm.is_current ===
    if "is_current" not in {c["name"] for c in insp.get_columns("academic_terms")}:
        op.add_column(
            "academic_terms",
            sa.Column(
                "is_current",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )

    existing_idx = {i["name"] for i in insp.get_indexes("academic_terms")}
    if "uq_academic_terms_is_current_singleton" not in existing_idx:
        op.create_index(
            "uq_academic_terms_is_current_singleton",
            "academic_terms",
            ["is_current"],
            unique=True,
            postgresql_where=sa.text("is_current = true"),
            sqlite_where=sa.text("is_current = 1"),
        )

    # === LeaveQuota.school_year ===
    if "school_year" not in {c["name"] for c in insp.get_columns("leave_quotas")}:
        op.add_column(
            "leave_quotas",
            sa.Column("school_year", sa.Integer(), nullable=True),
        )

    existing_idx_lq = {i["name"] for i in insp.get_indexes("leave_quotas")}
    if "uq_leave_quotas_employee_school_year_type" not in existing_idx_lq:
        op.create_index(
            "uq_leave_quotas_employee_school_year_type",
            "leave_quotas",
            ["employee_id", "school_year", "leave_type"],
            unique=True,
            postgresql_where=sa.text("school_year IS NOT NULL"),
            sqlite_where=sa.text("school_year IS NOT NULL"),
        )
    if "ix_leave_quotas_school_year" not in existing_idx_lq:
        op.create_index(
            "ix_leave_quotas_school_year",
            "leave_quotas",
            ["school_year"],
            postgresql_where=sa.text("school_year IS NOT NULL"),
            sqlite_where=sa.text("school_year IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    existing_idx_lq = {i["name"] for i in insp.get_indexes("leave_quotas")}
    if "ix_leave_quotas_school_year" in existing_idx_lq:
        op.drop_index("ix_leave_quotas_school_year", table_name="leave_quotas")
    if "uq_leave_quotas_employee_school_year_type" in existing_idx_lq:
        op.drop_index(
            "uq_leave_quotas_employee_school_year_type",
            table_name="leave_quotas",
        )
    if "school_year" in {c["name"] for c in insp.get_columns("leave_quotas")}:
        op.drop_column("leave_quotas", "school_year")

    existing_idx = {i["name"] for i in insp.get_indexes("academic_terms")}
    if "uq_academic_terms_is_current_singleton" in existing_idx:
        op.drop_index(
            "uq_academic_terms_is_current_singleton",
            table_name="academic_terms",
        )
    if "is_current" in {c["name"] for c in insp.get_columns("academic_terms")}:
        op.drop_column("academic_terms", "is_current")
