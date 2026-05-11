"""appraisal 考核系統初始化（6 表 + 8 enum 型別）

新增 8 個 enum + 6 張表：
- appraisal_cycles
- appraisal_participants
- appraisal_events
- appraisal_summaries
- appraisal_bonus_rates
- appraisal_penalty_catalog（seed 在後續 migration）

接在 user disciplinary_actions migration (u6v7w8x9y0z1) 之後。
disciplinary_actions 是金額型扣款（warning -1000 從節慶獎金扣），
appraisal_events 是分數型扣考核分（warning -2 影響學期等級），兩者互補。

Refs: docs/superpowers/specs/2026-05-11-employee-appraisal-system-design.md

Revision ID: v7w8x9y0z1a2
Revises: u6v7w8x9y0z1
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB

revision = "v7w8x9y0z1a2"
down_revision = "u6v7w8x9y0z1"
branch_labels = None
depends_on = None

# 用 postgresql.ENUM(create_type=False) 讓 create_table 不自動發 CREATE TYPE；
# 型別由 upgrade() 開頭以 CREATE TYPE … IF NOT EXISTS 手動建立。
SEMESTER = ENUM("FIRST", "SECOND", name="appraisal_semester_enum", create_type=False)
CYCLE_STATUS = ENUM(
    "OPEN", "LOCKED", "CLOSED", name="appraisal_cycle_status_enum", create_type=False
)
ROLE_GROUP = ENUM(
    "SUPERVISOR",
    "HEAD_TEACHER",
    "ASSISTANT",
    name="appraisal_role_group_enum",
    create_type=False,
)
EVENT_TYPE = ENUM(
    "MAJOR_MERIT",
    "MINOR_MERIT",
    "COMMENDATION",
    "WARNING",
    "MINOR_DEMERIT",
    "MAJOR_DEMERIT",
    "ORAL_WARNING",
    "SCORE_ADJUST",
    name="appraisal_event_type_enum",
    create_type=False,
)
PARENT_REACTION = ENUM(
    "none",
    "forgiven",
    "withdrawal",
    "litigation",
    "complaint",
    "media",
    name="appraisal_parent_reaction_enum",
    create_type=False,
)
GRADE = ENUM(
    "OUTSTANDING",
    "GOOD",
    "PASS",
    "WARN",
    "FAIL",
    name="appraisal_grade_enum",
    create_type=False,
)
SUMMARY_STATUS = ENUM(
    "DRAFT",
    "SUPERVISOR_SIGNED",
    "ACCOUNTING_SIGNED",
    "FINALIZED",
    name="appraisal_summary_status_enum",
    create_type=False,
)
CATALOG_CATEGORY = ENUM(
    "MISCONDUCT",
    "MEDICATION",
    "ACCIDENT",
    "DISPUTE",
    "NEGLIGENCE",
    "MERIT",
    "SPECIAL",
    name="appraisal_catalog_category_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # 以 raw DDL 建立全部 8 個 enum；用 DO $$ 包裝 + pg_type 檢查保證冪等
    # （PostgreSQL 沒有 CREATE TYPE IF NOT EXISTS 語法，必須走 plpgsql DO block）
    enum_ddls = [
        (
            "appraisal_semester_enum",
            "ENUM ('FIRST', 'SECOND')",
        ),
        (
            "appraisal_cycle_status_enum",
            "ENUM ('OPEN', 'LOCKED', 'CLOSED')",
        ),
        (
            "appraisal_role_group_enum",
            "ENUM ('SUPERVISOR', 'HEAD_TEACHER', 'ASSISTANT')",
        ),
        (
            "appraisal_event_type_enum",
            (
                "ENUM ('MAJOR_MERIT', 'MINOR_MERIT', 'COMMENDATION',"
                " 'WARNING', 'MINOR_DEMERIT', 'MAJOR_DEMERIT',"
                " 'ORAL_WARNING', 'SCORE_ADJUST')"
            ),
        ),
        (
            "appraisal_parent_reaction_enum",
            "ENUM ('none', 'forgiven', 'withdrawal', 'litigation', 'complaint', 'media')",
        ),
        (
            "appraisal_grade_enum",
            "ENUM ('OUTSTANDING', 'GOOD', 'PASS', 'WARN', 'FAIL')",
        ),
        (
            "appraisal_summary_status_enum",
            "ENUM ('DRAFT', 'SUPERVISOR_SIGNED', 'ACCOUNTING_SIGNED', 'FINALIZED')",
        ),
        (
            "appraisal_catalog_category_enum",
            (
                "ENUM ('MISCONDUCT', 'MEDICATION', 'ACCIDENT', 'DISPUTE',"
                " 'NEGLIGENCE', 'MERIT', 'SPECIAL')"
            ),
        ),
    ]
    for type_name, enum_def in enum_ddls:
        op.execute(
            f"DO $$ BEGIN"
            f"  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{type_name}')"
            f"  THEN CREATE TYPE {type_name} AS {enum_def}; END IF;"
            f" END $$"
        )

    if "appraisal_cycles" not in tables:
        op.create_table(
            "appraisal_cycles",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("academic_year", sa.Integer(), nullable=False),
            sa.Column("semester", SEMESTER, nullable=False),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("end_date", sa.Date(), nullable=False),
            sa.Column("base_score_calc_date", sa.Date(), nullable=False),
            sa.Column("status", CYCLE_STATUS, nullable=False, server_default="OPEN"),
            sa.Column(
                "created_by",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "academic_year", "semester", name="uq_appraisal_cycle_year_sem"
            ),
        )

    if "appraisal_penalty_catalog" not in tables:
        op.create_table(
            "appraisal_penalty_catalog",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("code", sa.String(40), nullable=False, unique=True),
            sa.Column("category", CATALOG_CATEGORY, nullable=False),
            sa.Column("subcategory", sa.String(60), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("default_event_type", EVENT_TYPE, nullable=False),
            sa.Column("default_score_delta", sa.Numeric(4, 1), nullable=False),
            sa.Column(
                "severity_max", sa.SmallInteger(), nullable=False, server_default="1"
            ),
            sa.Column(
                "display_order", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
        )

    if "appraisal_participants" not in tables:
        op.create_table(
            "appraisal_participants",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column(
                "cycle_id",
                sa.BigInteger(),
                sa.ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "employee_id",
                sa.Integer(),
                sa.ForeignKey("employees.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("role_group", ROLE_GROUP, nullable=False),
            sa.Column(
                "classroom_id",
                sa.Integer(),
                sa.ForeignKey("classrooms.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "base_score", sa.Numeric(5, 2), nullable=False, server_default="0"
            ),
            sa.Column("target_enrollment", sa.Integer(), nullable=True),
            sa.Column("actual_enrollment", sa.Integer(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "cycle_id", "employee_id", name="uq_appraisal_participant_cycle_emp"
            ),
        )
        op.create_index(
            "ix_appraisal_participant_cycle_rg",
            "appraisal_participants",
            ["cycle_id", "role_group"],
        )

    if "appraisal_events" not in tables:
        op.create_table(
            "appraisal_events",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column(
                "participant_id",
                sa.BigInteger(),
                sa.ForeignKey("appraisal_participants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "cycle_id",
                sa.BigInteger(),
                sa.ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "catalog_item_id",
                sa.BigInteger(),
                sa.ForeignKey("appraisal_penalty_catalog.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("event_type", EVENT_TYPE, nullable=False),
            sa.Column("event_date", sa.Date(), nullable=False),
            sa.Column("score_delta", sa.Numeric(4, 1), nullable=False),
            sa.Column("severity_level", sa.SmallInteger(), nullable=True),
            sa.Column("parent_reaction", PARENT_REACTION, nullable=True),
            sa.Column("title", sa.String(120), nullable=False),
            sa.Column("detail", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "attachments",
                JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column(
                "created_by",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("reverted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "reverted_by",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("reverted_reason", sa.Text(), nullable=True),
        )
        # spec §4.3 規定 (participant_id, event_date DESC)；
        # op.create_index 不支援 sort direction，改用 raw DDL
        op.execute(
            "CREATE INDEX ix_appraisal_event_participant_date "
            "ON appraisal_events (participant_id, event_date DESC)"
        )
        op.create_index(
            "ix_appraisal_event_cycle_type",
            "appraisal_events",
            ["cycle_id", "event_type"],
        )
        op.create_index(
            "ix_appraisal_event_created",
            "appraisal_events",
            ["created_by", "created_at"],
        )
        # spec §4.3 catalog 統計用索引
        op.create_index(
            "ix_appraisal_event_catalog_item",
            "appraisal_events",
            ["catalog_item_id"],
        )

    if "appraisal_summaries" not in tables:
        op.create_table(
            "appraisal_summaries",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column(
                "participant_id",
                sa.BigInteger(),
                sa.ForeignKey("appraisal_participants.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column(
                "cycle_id",
                sa.BigInteger(),
                sa.ForeignKey("appraisal_cycles.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("base_score", sa.Numeric(5, 2), nullable=False),
            sa.Column(
                "event_score_sum",
                sa.Numeric(5, 2),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "total_score", sa.Numeric(5, 2), nullable=False, server_default="0"
            ),
            sa.Column("grade", GRADE, nullable=False, server_default="FAIL"),
            sa.Column(
                "bonus_amount", sa.Numeric(10, 2), nullable=False, server_default="0"
            ),
            sa.Column("status", SUMMARY_STATUS, nullable=False, server_default="DRAFT"),
            sa.Column(
                "supervisor_signed_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column(
                "supervisor_signed_by",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("supervisor_comment", sa.Text(), nullable=True),
            sa.Column(
                "accounting_signed_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column(
                "accounting_signed_by",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("accounting_comment", sa.Text(), nullable=True),
            sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "finalized_by",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("finalized_comment", sa.Text(), nullable=True),
            sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "rejected_by",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("rejected_from_stage", SUMMARY_STATUS, nullable=True),
            sa.Column("rejected_reason", sa.Text(), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_appraisal_summary_cycle_status",
            "appraisal_summaries",
            ["cycle_id", "status"],
        )
        op.create_index(
            "ix_appraisal_summary_cycle_grade",
            "appraisal_summaries",
            ["cycle_id", "grade"],
        )

    if "appraisal_bonus_rates" not in tables:
        op.create_table(
            "appraisal_bonus_rates",
            sa.Column("id", sa.BigInteger(), primary_key=True),
            sa.Column("effective_from", sa.Date(), nullable=False),
            sa.Column("role_group", ROLE_GROUP, nullable=False),
            sa.Column("grade", GRADE, nullable=False),
            sa.Column("base_amount", sa.Numeric(10, 2), nullable=False),
            sa.Column(
                "created_by",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "effective_from", "role_group", "grade", name="uq_appraisal_bonus_rate"
            ),
        )


def downgrade() -> None:
    op.drop_table("appraisal_bonus_rates")
    op.drop_index("ix_appraisal_summary_cycle_grade", table_name="appraisal_summaries")
    op.drop_index("ix_appraisal_summary_cycle_status", table_name="appraisal_summaries")
    op.drop_table("appraisal_summaries")
    op.drop_index("ix_appraisal_event_catalog_item", table_name="appraisal_events")
    op.drop_index("ix_appraisal_event_created", table_name="appraisal_events")
    op.drop_index("ix_appraisal_event_cycle_type", table_name="appraisal_events")
    # I2: 對應 upgrade 的 raw DDL（op.drop_index 無法清掉非 alembic 建的索引名稱時，
    # 用 raw DROP INDEX IF EXISTS 較保險）
    op.execute("DROP INDEX IF EXISTS ix_appraisal_event_participant_date")
    op.drop_table("appraisal_events")
    op.drop_index(
        "ix_appraisal_participant_cycle_rg", table_name="appraisal_participants"
    )
    op.drop_table("appraisal_participants")
    op.drop_table("appraisal_penalty_catalog")
    op.drop_table("appraisal_cycles")

    for type_name in (
        "appraisal_catalog_category_enum",
        "appraisal_summary_status_enum",
        "appraisal_grade_enum",
        "appraisal_parent_reaction_enum",
        "appraisal_event_type_enum",
        "appraisal_role_group_enum",
        "appraisal_cycle_status_enum",
        "appraisal_semester_enum",
    ):
        op.execute(f"DROP TYPE IF EXISTS {type_name}")
