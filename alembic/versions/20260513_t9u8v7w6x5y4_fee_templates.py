"""fee_templates + fee record/refund extras (學費管理系統)

新增 fee_templates 表(年級×學年×學期×費用類型),
StudentFeeRecord 加 fee_type/source_template_id/target_month,
StudentFeeRefund 加 calc_method/calc_payload。

Revision ID: t9u8v7w6x5y4
Revises: g6h7i8j9k0l1
Create Date: 2026-05-13
"""

from alembic import op
import sqlalchemy as sa

revision = "t9u8v7w6x5y4"
down_revision = "g6h7i8j9k0l1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fee_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "grade_id",
            sa.Integer(),
            sa.ForeignKey("class_grades.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("school_year", sa.Integer(), nullable=False, comment="民國年"),
        sa.Column("semester", sa.Integer(), nullable=False, comment="1=上,2=下"),
        sa.Column(
            "fee_type",
            sa.String(20),
            nullable=False,
            comment="registration/miscellaneous/monthly",
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("breakdown", sa.JSON(), nullable=True),
        sa.Column(
            "due_date_offset_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("14"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("created_by", sa.String(50), nullable=True),
        sa.Column("updated_by", sa.String(50), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "grade_id",
            "school_year",
            "semester",
            "fee_type",
            name="uq_fee_template",
        ),
        sa.CheckConstraint(
            "fee_type IN ('registration','miscellaneous','monthly')",
            name="ck_fee_template_type",
        ),
        sa.CheckConstraint("amount >= 0", name="ck_fee_template_amount_nonneg"),
        sa.CheckConstraint("semester IN (1, 2)", name="ck_fee_template_semester"),
    )
    op.create_index(
        "ix_fee_templates_term_active",
        "fee_templates",
        ["school_year", "semester", "is_active"],
    )

    # StudentFeeRecord 擴充
    op.add_column(
        "student_fee_records",
        sa.Column("fee_type", sa.String(20), nullable=True),
    )
    op.add_column(
        "student_fee_records",
        sa.Column(
            "source_template_id",
            sa.Integer(),
            sa.ForeignKey("fee_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "student_fee_records",
        sa.Column("target_month", sa.String(7), nullable=True),
    )
    op.create_index(
        "ix_fee_records_fee_type",
        "student_fee_records",
        ["fee_type"],
    )
    # 月費冪等鍵:同學生同範本同月份只能一張
    op.create_index(
        "ix_fee_records_monthly_unique",
        "student_fee_records",
        ["student_id", "source_template_id", "target_month"],
        unique=True,
        postgresql_where=sa.text(
            "source_template_id IS NOT NULL AND target_month IS NOT NULL"
        ),
    )

    # StudentFeeRefund 擴充:計算明細
    op.add_column(
        "student_fee_refunds",
        sa.Column("calc_method", sa.String(30), nullable=True),
    )
    op.add_column(
        "student_fee_refunds",
        sa.Column("calc_payload", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("student_fee_refunds", "calc_payload")
    op.drop_column("student_fee_refunds", "calc_method")
    op.drop_index("ix_fee_records_monthly_unique", table_name="student_fee_records")
    op.drop_index("ix_fee_records_fee_type", table_name="student_fee_records")
    op.drop_column("student_fee_records", "target_month")
    op.drop_column("student_fee_records", "source_template_id")
    op.drop_column("student_fee_records", "fee_type")
    op.drop_index("ix_fee_templates_term_active", table_name="fee_templates")
    op.drop_table("fee_templates")
