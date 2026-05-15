"""appraisal + year_end 系統初始化（M1 重構版）

對映 Excel「114(上)年度考核統計表」16 項加減分結構與「114年年終經營績效」22 sheets 結算流程。

新版表結構：
半年考核 6 表
- appraisal_cycles               學期週期（含整 cycle base_score、招生目標）
- appraisal_participants         每人 + 角色 + 班級 + 到職月數
- appraisal_score_item_catalog   16 項定義（取代舊 appraisal_penalty_catalog）
- appraisal_score_items          每 participant × item 加減分（取代舊 appraisal_events）
- appraisal_summaries            合計 + 等第 + 獎金 + 簽核
- appraisal_bonus_rates          (生效日, 角色群, 等第) → 底數

年終獎金 6 表
- year_end_cycles                每年一筆
- org_year_settings              每學期一筆全校設定
- class_enrollment_targets       班級每學期編制與目標
- employee_year_end_snapshot     每員工每 cycle snapshot
- year_end_settlements           每員工結算單（6 層計算）
- special_bonus_items            8 種特別獎金統一表

舊版 appraisal_events / appraisal_penalty_catalog 表與
appraisal_event_type_enum / appraisal_parent_reaction_enum /
appraisal_catalog_category_enum 三個 enum 已棄用，於本 migration 開頭 DROP IF EXISTS。

⚠ 既有 dev DB 已執行過舊版 a1p2p3r4i5s6：請先 `alembic downgrade v8a9b0c1d2e3`
   再 `alembic upgrade head`，或直接 reset DB；本 migration 內含 IF EXISTS 保護。

Revision ID: a1p2p3r4i5s6
Revises: v8a9b0c1d2e3
Create Date: 2026-05-11 (rewritten 2026-05-15 for M1)
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB

revision = "a1p2p3r4i5s6"
down_revision = "v8a9b0c1d2e3"
branch_labels = None
depends_on = None


# === 新 enum 定義（PG ENUM；create_type=False 因由 upgrade() raw DDL 手動建立）===
SEMESTER = ENUM(
    "FIRST", "SECOND", name="appraisal_semester_enum", create_type=False
)
CYCLE_STATUS = ENUM(
    "OPEN", "LOCKED", "CLOSED", name="appraisal_cycle_status_enum", create_type=False
)
ROLE_GROUP = ENUM(
    "SUPERVISOR",
    "HEAD_TEACHER",
    "ASSISTANT",
    "STAFF",
    "COOK",
    name="appraisal_role_group_enum",
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
SCORE_ITEM_SIGN = ENUM(
    "POSITIVE",
    "NEGATIVE",
    "NEUTRAL",
    name="appraisal_score_item_sign_enum",
    create_type=False,
)
YEAR_END_CYCLE_STATUS = ENUM(
    "OPEN",
    "LOCKED",
    "CLOSED",
    name="year_end_cycle_status_enum",
    create_type=False,
)
YEAR_END_SETTLEMENT_STATUS = ENUM(
    "DRAFT",
    "SUPERVISOR_SIGNED",
    "ACCOUNTING_SIGNED",
    "FINALIZED",
    name="year_end_settlement_status_enum",
    create_type=False,
)
SPECIAL_BONUS_TYPE = ENUM(
    "APPRAISAL_HALF_BONUS_FIRST",
    "APPRAISAL_HALF_BONUS_SECOND",
    "SEMESTER_DIVIDEND_FIRST",
    "SEMESTER_DIVIDEND_SECOND",
    "AFTER_CLASS_AWARD",
    "TEACHING_EXTRA",
    "EXCESS_ENROLLMENT",
    "FESTIVAL_DIFF",
    "CUSTOM",
    name="year_end_special_bonus_type_enum",
    create_type=False,
)


# ===== 待清理的舊 enum 與表（M1 重構） =====
OBSOLETE_TABLES = ["appraisal_events", "appraisal_penalty_catalog"]
OBSOLETE_ENUMS = [
    "appraisal_event_type_enum",
    "appraisal_parent_reaction_enum",
    "appraisal_catalog_category_enum",
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # --- Step 0: 清理舊版本（如果上一次 a1p2p3r4i5s6 已執行）---
    for tbl in OBSOLETE_TABLES:
        if tbl in tables:
            op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
    for tbl in [
        "appraisal_summaries",
        "appraisal_bonus_rates",
        "appraisal_participants",
        "appraisal_cycles",
    ]:
        if tbl in tables:
            op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
    # 重新 inspect（前面 drop 過了）
    tables = set(sa.inspect(bind).get_table_names())

    for enum_name in OBSOLETE_ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")

    # 為防 ALTER TYPE ... ADD VALUE 不冪等的問題，舊 enum 也一併 drop 重建
    for enum_name in [
        "appraisal_role_group_enum",
        "appraisal_semester_enum",
        "appraisal_cycle_status_enum",
        "appraisal_grade_enum",
        "appraisal_summary_status_enum",
        "appraisal_score_item_sign_enum",
        "year_end_cycle_status_enum",
        "year_end_settlement_status_enum",
        "year_end_special_bonus_type_enum",
    ]:
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")

    # --- Step 1: 建立新 enum types ---
    enum_ddls = [
        ("appraisal_semester_enum", "ENUM ('FIRST', 'SECOND')"),
        ("appraisal_cycle_status_enum", "ENUM ('OPEN', 'LOCKED', 'CLOSED')"),
        (
            "appraisal_role_group_enum",
            "ENUM ('SUPERVISOR', 'HEAD_TEACHER', 'ASSISTANT', 'STAFF', 'COOK')",
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
            "appraisal_score_item_sign_enum",
            "ENUM ('POSITIVE', 'NEGATIVE', 'NEUTRAL')",
        ),
        ("year_end_cycle_status_enum", "ENUM ('OPEN', 'LOCKED', 'CLOSED')"),
        (
            "year_end_settlement_status_enum",
            "ENUM ('DRAFT', 'SUPERVISOR_SIGNED', 'ACCOUNTING_SIGNED', 'FINALIZED')",
        ),
        (
            "year_end_special_bonus_type_enum",
            (
                "ENUM ('APPRAISAL_HALF_BONUS_FIRST', 'APPRAISAL_HALF_BONUS_SECOND',"
                " 'SEMESTER_DIVIDEND_FIRST', 'SEMESTER_DIVIDEND_SECOND',"
                " 'AFTER_CLASS_AWARD', 'TEACHING_EXTRA', 'EXCESS_ENROLLMENT',"
                " 'FESTIVAL_DIFF', 'CUSTOM')"
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

    # --- Step 2: 半年考核 6 表 ---

    # 2.1 appraisal_cycles
    op.create_table(
        "appraisal_cycles",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("academic_year", sa.Integer(), nullable=False),
        sa.Column("semester", SEMESTER, nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("base_score_calc_date", sa.Date(), nullable=False),
        sa.Column("base_score", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("enrollment_target", sa.Integer(), nullable=True),
        sa.Column("enrollment_actual", sa.Integer(), nullable=True),
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

    # 2.2 appraisal_score_item_catalog
    op.create_table(
        "appraisal_score_item_catalog",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("sign", SCORE_ITEM_SIGN, nullable=False),
        sa.Column(
            "default_weight", sa.Numeric(4, 1), nullable=False, server_default="0"
        ),
        sa.Column("data_source", sa.String(60), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    # 2.3 appraisal_participants
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
            "hire_months_in_cycle",
            sa.Numeric(4, 1),
            nullable=False,
            server_default="6",
        ),
        sa.Column(
            "is_excluded", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("exclude_reason", sa.String(120), nullable=True),
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

    # 2.4 appraisal_score_items
    op.create_table(
        "appraisal_score_items",
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
            "catalog_id",
            sa.BigInteger(),
            sa.ForeignKey("appraisal_score_item_catalog.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("item_code", sa.String(40), nullable=False),
        sa.Column(
            "sequence_no", sa.SmallInteger(), nullable=False, server_default="1"
        ),
        sa.Column("score_delta", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("raw_value", sa.Numeric(8, 2), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source_ref", sa.String(60), nullable=True),
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
            "participant_id",
            "item_code",
            "sequence_no",
            name="uq_appraisal_score_item_unique",
        ),
    )
    op.create_index(
        "ix_appraisal_score_item_cycle_code",
        "appraisal_score_items",
        ["cycle_id", "item_code"],
    )

    # 2.5 appraisal_summaries
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
            "event_score_sum", sa.Numeric(6, 2), nullable=False, server_default="0"
        ),
        sa.Column(
            "total_score", sa.Numeric(6, 2), nullable=False, server_default="0"
        ),
        sa.Column("grade", GRADE, nullable=False, server_default="FAIL"),
        sa.Column(
            "bonus_amount", sa.Numeric(10, 2), nullable=False, server_default="0"
        ),
        sa.Column("leave_note", sa.String(120), nullable=True),
        sa.Column("status", SUMMARY_STATUS, nullable=False, server_default="DRAFT"),
        sa.Column("supervisor_signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "supervisor_signed_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("supervisor_comment", sa.Text(), nullable=True),
        sa.Column("accounting_signed_at", sa.DateTime(timezone=True), nullable=True),
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

    # 2.6 appraisal_bonus_rates
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

    # --- Step 3: 年終獎金 6 表 ---

    # 3.1 year_end_cycles
    op.create_table(
        "year_end_cycles",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("academic_year", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("bonus_calc_date", sa.Date(), nullable=False),
        sa.Column(
            "status", YEAR_END_CYCLE_STATUS, nullable=False, server_default="OPEN"
        ),
        sa.Column(
            "params_snapshot",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
        sa.UniqueConstraint("academic_year", name="uq_year_end_cycle_year"),
    )

    # 3.2 org_year_settings
    op.create_table(
        "org_year_settings",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "year_end_cycle_id",
            sa.BigInteger(),
            sa.ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("semester_first", sa.Boolean(), nullable=False),
        sa.Column("enrollment_target", sa.Integer(), nullable=False),
        sa.Column("enrollment_actual", sa.Integer(), nullable=True),
        sa.Column(
            "school_achievement_rate",
            sa.Numeric(6, 3),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "org_achievement_rate",
            sa.Numeric(6, 3),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "meeting_absence_deduction",
            sa.Numeric(8, 2),
            nullable=False,
            server_default="1000",
        ),
        sa.Column(
            "festival_bonus_meta",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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
            "year_end_cycle_id", "semester_first", name="uq_org_year_settings_sem"
        ),
    )

    # 3.3 class_enrollment_targets
    op.create_table(
        "class_enrollment_targets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "year_end_cycle_id",
            sa.BigInteger(),
            sa.ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("semester_first", sa.Boolean(), nullable=False),
        sa.Column(
            "classroom_id",
            sa.Integer(),
            sa.ForeignKey("classrooms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "head_teacher_employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "assistant_employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("head_count_target", sa.Integer(), nullable=False),
        sa.Column(
            "avg_monthly_enrollment",
            sa.Numeric(6, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "class_performance_rate",
            sa.Numeric(6, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "returning_student_rate",
            sa.Numeric(6, 3),
            nullable=False,
            server_default="0",
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
            "year_end_cycle_id",
            "semester_first",
            "classroom_id",
            name="uq_class_enrollment_target",
        ),
    )

    # 3.4 employee_year_end_snapshot
    op.create_table(
        "employee_year_end_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "year_end_cycle_id",
            sa.BigInteger(),
            sa.ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("base_salary", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column(
            "festival_total", sa.Numeric(10, 2), nullable=False, server_default="0"
        ),
        sa.Column("role", sa.String(40), nullable=True),
        sa.Column(
            "classroom_id",
            sa.Integer(),
            sa.ForeignKey("classrooms.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("hire_date", sa.Date(), nullable=True),
        sa.Column("resign_date", sa.Date(), nullable=True),
        sa.Column("hire_months", sa.Numeric(4, 1), nullable=False, server_default="12"),
        sa.Column(
            "is_resigned", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "is_contracted", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column(
            "extra",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
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
            "year_end_cycle_id",
            "employee_id",
            name="uq_employee_year_end_snapshot",
        ),
    )

    # 3.5 year_end_settlements
    op.create_table(
        "year_end_settlements",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "year_end_cycle_id",
            sa.BigInteger(),
            sa.ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_id",
            sa.BigInteger(),
            sa.ForeignKey("employee_year_end_snapshot.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # step1 平均績效
        sa.Column("school_rate_first", sa.Numeric(6, 2), nullable=True),
        sa.Column("school_rate_second", sa.Numeric(6, 2), nullable=True),
        sa.Column("class_returning_rate_first", sa.Numeric(6, 2), nullable=True),
        sa.Column("class_returning_rate_second", sa.Numeric(6, 2), nullable=True),
        sa.Column("class_performance_rate_first", sa.Numeric(6, 2), nullable=True),
        sa.Column("class_performance_rate_second", sa.Numeric(6, 2), nullable=True),
        sa.Column(
            "avg_performance_rate",
            sa.Numeric(6, 2),
            nullable=False,
            server_default="0",
        ),
        # step2 毛額
        sa.Column("base_salary", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column(
            "festival_total", sa.Numeric(10, 2), nullable=False, server_default="0"
        ),
        sa.Column("gross_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        # step3 小計
        sa.Column(
            "org_achievement_rate",
            sa.Numeric(6, 3),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "subtotal_amount", sa.Numeric(10, 2), nullable=False, server_default="0"
        ),
        # step4 扣項
        sa.Column(
            "deduction_leave_late",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "deduction_meeting", sa.Numeric(10, 2), nullable=False, server_default="0"
        ),
        sa.Column(
            "deduction_personal_leave",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "deduction_sick_leave",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "deduction_late", sa.Numeric(10, 2), nullable=False, server_default="0"
        ),
        sa.Column(
            "deduction_disciplinary",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "deduction_total", sa.Numeric(10, 2), nullable=False, server_default="0"
        ),
        # step5 應領小計
        sa.Column("hire_months", sa.Numeric(4, 1), nullable=False, server_default="12"),
        sa.Column(
            "proration_rate", sa.Numeric(5, 4), nullable=False, server_default="1"
        ),
        sa.Column(
            "payable_amount", sa.Numeric(10, 2), nullable=False, server_default="0"
        ),
        # step6 年終總額
        sa.Column(
            "special_bonus_total",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_amount", sa.Numeric(10, 2), nullable=False, server_default="0"
        ),
        sa.Column(
            "calc_meta",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column(
            "status",
            YEAR_END_SETTLEMENT_STATUS,
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("supervisor_signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "supervisor_signed_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("accounting_signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "accounting_signed_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "finalized_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "rejected_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "rejected_from_stage", YEAR_END_SETTLEMENT_STATUS, nullable=True
        ),
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
        sa.UniqueConstraint(
            "year_end_cycle_id",
            "employee_id",
            name="uq_year_end_settlement_cycle_emp",
        ),
    )
    op.create_index(
        "ix_year_end_settlement_cycle_status",
        "year_end_settlements",
        ["year_end_cycle_id", "status"],
    )

    # 3.6 special_bonus_items
    op.create_table(
        "special_bonus_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "year_end_cycle_id",
            sa.BigInteger(),
            sa.ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "employee_id",
            sa.Integer(),
            sa.ForeignKey("employees.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("bonus_type", SPECIAL_BONUS_TYPE, nullable=False),
        sa.Column("period_label", sa.String(40), nullable=False, server_default=""),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column(
            "classroom_id",
            sa.Integer(),
            sa.ForeignKey("classrooms.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "calc_meta",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("source_ref", sa.String(120), nullable=True),
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
            "year_end_cycle_id",
            "employee_id",
            "bonus_type",
            "period_label",
            name="uq_special_bonus_item",
        ),
    )
    op.create_index(
        "ix_special_bonus_item_emp_cycle",
        "special_bonus_items",
        ["employee_id", "year_end_cycle_id"],
    )


def downgrade() -> None:
    # 反向順序：先 drop year_end 6 表，再 drop appraisal 6 表，最後 drop enum
    op.drop_index("ix_special_bonus_item_emp_cycle", table_name="special_bonus_items")
    op.drop_table("special_bonus_items")
    op.drop_index(
        "ix_year_end_settlement_cycle_status", table_name="year_end_settlements"
    )
    op.drop_table("year_end_settlements")
    op.drop_table("employee_year_end_snapshot")
    op.drop_table("class_enrollment_targets")
    op.drop_table("org_year_settings")
    op.drop_table("year_end_cycles")

    op.drop_table("appraisal_bonus_rates")
    op.drop_index(
        "ix_appraisal_summary_cycle_grade", table_name="appraisal_summaries"
    )
    op.drop_index(
        "ix_appraisal_summary_cycle_status", table_name="appraisal_summaries"
    )
    op.drop_table("appraisal_summaries")
    op.drop_index(
        "ix_appraisal_score_item_cycle_code", table_name="appraisal_score_items"
    )
    op.drop_table("appraisal_score_items")
    op.drop_index(
        "ix_appraisal_participant_cycle_rg", table_name="appraisal_participants"
    )
    op.drop_table("appraisal_participants")
    op.drop_table("appraisal_score_item_catalog")
    op.drop_table("appraisal_cycles")

    for type_name in (
        "year_end_special_bonus_type_enum",
        "year_end_settlement_status_enum",
        "year_end_cycle_status_enum",
        "appraisal_score_item_sign_enum",
        "appraisal_summary_status_enum",
        "appraisal_grade_enum",
        "appraisal_role_group_enum",
        "appraisal_cycle_status_enum",
        "appraisal_semester_enum",
    ):
        op.execute(f"DROP TYPE IF EXISTS {type_name}")
