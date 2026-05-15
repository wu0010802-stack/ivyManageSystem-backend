"""appraisal + year_end 重構初始化（半年考核 + 年終獎金）

完全取代舊版 appraisal_init（已從 alembic graph 移除）：
- 砍 4 條舊 migration（a1p2p3r4i5s6 / a3p4p5r6i7s8 / a7p8p9r0i1s2 / a9p0p1r2i3s4）
- 移除 `appraisal_events` / `appraisal_penalty_catalog` 兩表（如存在）
- DROP + 重建 8 個舊 enum，並補 `appraisal_score_item_sign_enum` / `year_end_*` 系列
- 新增 6 個半年考核表（cycles, participants, score_item_catalog, score_items,
  summaries, bonus_rates）+ 6 個年終獎金表
- seed 16 條 score_item_catalog + 6 條 bonus_rates

設計依據：plans/here-is-a-draft-vast-scone.md

Revision ID: b2c3d4e5f6a7
Revises: ar1n3c1d4l1nk, v8a9b0c1d2e3
Create Date: 2026-05-15

備註：同時 merge 兩條鏈 — 原 main 鏈尾 ar1n3c1d4l1nk + 因移除舊 appraisal_init
而孤立的 moe_phase1_schema (v8a9b0c1d2e3)，避免出現兩個 heads。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB

revision = "b2c3d4e5f6a7"
down_revision = ("ar1n3c1d4l1nk", "v8a9b0c1d2e3")
branch_labels = None
depends_on = None


# === 半年考核 enum ===
SEMESTER = ENUM("FIRST", "SECOND", name="appraisal_semester_enum", create_type=False)
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
    "PLUS",
    "MINUS",
    "BOTH",
    name="appraisal_score_item_sign_enum",
    create_type=False,
)

# === 年終獎金 enum ===
YEAR_END_STATUS = ENUM(
    "DRAFT",
    "CALCULATED",
    "FINALIZED",
    "PAID",
    name="year_end_cycle_status_enum",
    create_type=False,
)
SPECIAL_BONUS_TYPE = ENUM(
    "AFTER_CLASS_AWARD",  # 課後才藝鼓勵獎金
    "EXCESS_ENROLLMENT",  # 超額獎金
    "TEACHING_EXTRA",  # 教課教師獎勵金
    "SEMESTER_DIVIDEND_113_1",  # 113 上學期紅利
    "SEMESTER_DIVIDEND_113_2",  # 113 下學期紅利
    "FESTIVAL_DIFF",  # 節慶獎金比例差額（可為負）
    "BIRTHDAY",  # 壽星獎金
    "OTHER",  # 其他自訂
    name="year_end_special_bonus_type_enum",
    create_type=False,
)
SETTLEMENT_STATUS = ENUM(
    "DRAFT",
    "CALCULATED",
    "REVIEWED",
    "FINALIZED",
    name="year_end_settlement_status_enum",
    create_type=False,
)


ENUM_DDLS = [
    ("appraisal_semester_enum", "ENUM ('FIRST', 'SECOND')"),
    ("appraisal_cycle_status_enum", "ENUM ('OPEN', 'LOCKED', 'CLOSED')"),
    (
        "appraisal_role_group_enum",
        "ENUM ('SUPERVISOR', 'HEAD_TEACHER', 'ASSISTANT', 'STAFF', 'COOK')",
    ),
    ("appraisal_grade_enum", "ENUM ('OUTSTANDING', 'GOOD', 'PASS', 'WARN', 'FAIL')"),
    (
        "appraisal_summary_status_enum",
        "ENUM ('DRAFT', 'SUPERVISOR_SIGNED', 'ACCOUNTING_SIGNED', 'FINALIZED')",
    ),
    ("appraisal_score_item_sign_enum", "ENUM ('PLUS', 'MINUS', 'BOTH')"),
    (
        "year_end_cycle_status_enum",
        "ENUM ('DRAFT', 'CALCULATED', 'FINALIZED', 'PAID')",
    ),
    (
        "year_end_special_bonus_type_enum",
        (
            "ENUM ('AFTER_CLASS_AWARD', 'EXCESS_ENROLLMENT', 'TEACHING_EXTRA', "
            "'SEMESTER_DIVIDEND_113_1', 'SEMESTER_DIVIDEND_113_2', "
            "'FESTIVAL_DIFF', 'BIRTHDAY', 'OTHER')"
        ),
    ),
    (
        "year_end_settlement_status_enum",
        "ENUM ('DRAFT', 'CALCULATED', 'REVIEWED', 'FINALIZED')",
    ),
]


# === 16 條 score_item_catalog seed ===
# (code, label, sign, default_weight, data_source)
SCORE_ITEM_CATALOG_SEED = [
    ("LEAVE", "請休假", "MINUS", 1.0, "leaves"),
    ("LATE_EARLY", "遲到早退", "MINUS", 0.5, "attendance"),
    ("NO_CLOCK", "未打卡", "MINUS", 0.5, "attendance"),
    ("MISS_PRESCHOOL_MEETING", "園務會議未參加", "MINUS", 1.0, "meetings"),
    ("ORG_MEETING_0913", "9/13 機構會議", "MINUS", 1.0, "manual"),
    ("ORG_MEETING_1115", "11/15 機構會議", "MINUS", 1.0, "manual"),
    ("TEAM_ACTIVITY_1115", "11/15 自強活動", "MINUS", 1.0, "manual"),
    ("DROPOUT_0915", "9/15 休學人數", "MINUS", 1.0, "students"),
    ("DROPOUT_0315", "3/15 休學人數", "MINUS", 1.0, "students"),
    ("CHILD_INCIDENT", "幼兒意外", "MINUS", 1.0, "manual"),
    ("RETURNING_RATE_0315", "3/15 舊生註冊率", "PLUS", 1.0, "students"),
    ("CLASS_SIZE", "帶班人數加分", "PLUS", 1.0, "classroom"),
    ("AFTER_CLASS_RATE", "才藝班參加率", "PLUS", 1.0, "activity"),
    ("SPED", "特教生加分", "PLUS", 2.0, "students"),
    ("REWARD_PUNISH", "獎懲（大過/嘉獎）", "BOTH", 1.0, "manual"),
    ("OTHER_ADJUST", "其他主管調整", "BOTH", 1.0, "manual"),
]


# === 6 條 bonus_rates seed ===
# (effective_from, role_group, grade, base_amount)
BONUS_RATES_SEED = [
    ("2026-08-01", "SUPERVISOR", "OUTSTANDING", 10000),
    ("2026-08-01", "SUPERVISOR", "GOOD", 5000),
    ("2026-08-01", "HEAD_TEACHER", "OUTSTANDING", 8000),
    ("2026-08-01", "HEAD_TEACHER", "GOOD", 4000),
    ("2026-08-01", "ASSISTANT", "OUTSTANDING", 6000),
    ("2026-08-01", "ASSISTANT", "GOOD", 3500),
    ("2026-08-01", "STAFF", "OUTSTANDING", 6000),
    ("2026-08-01", "STAFF", "GOOD", 3500),
    ("2026-08-01", "COOK", "OUTSTANDING", 6000),
    ("2026-08-01", "COOK", "GOOD", 3500),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ── Step 1: drop 舊表（appraisal_events / appraisal_penalty_catalog） ──
    # 既有 appraisal_init / appraisal_seed_* migrations 已從 graph 砍除，
    # 但若 DB 已 upgrade 過該批 migration，殘留的表/enum 需在此清乾淨。
    for old_tbl in (
        "appraisal_events",
        "appraisal_penalty_catalog",
        "appraisal_summaries",
        "appraisal_bonus_rates",
        "appraisal_participants",
        "appraisal_cycles",
    ):
        if old_tbl in tables:
            op.execute(f"DROP TABLE IF EXISTS {old_tbl} CASCADE")

    # ── Step 2: drop 既有 enum（含舊版 event_type / parent_reaction / catalog_category） ──
    for old_enum in (
        "appraisal_event_type_enum",
        "appraisal_parent_reaction_enum",
        "appraisal_catalog_category_enum",
        "appraisal_semester_enum",
        "appraisal_cycle_status_enum",
        "appraisal_role_group_enum",
        "appraisal_grade_enum",
        "appraisal_summary_status_enum",
    ):
        op.execute(f"DROP TYPE IF EXISTS {old_enum} CASCADE")

    # ── Step 3: 建立全部新 enum（idempotent） ──
    for type_name, enum_def in ENUM_DDLS:
        op.execute(
            f"DO $$ BEGIN"
            f"  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{type_name}')"
            f"  THEN CREATE TYPE {type_name} AS {enum_def}; END IF;"
            f" END $$"
        )

    # ── Step 4: 半年考核表 ────────────────────────────────────────────────
    op.create_table(
        "appraisal_cycles",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("academic_year", sa.Integer(), nullable=False),
        sa.Column("semester", SEMESTER, nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("base_score_calc_date", sa.Date(), nullable=False),
        sa.Column(
            "base_score",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="0",
            comment="全校基礎分（actual_enrollment / target × 100）",
        ),
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

    op.create_table(
        "appraisal_score_item_catalog",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("sign", SCORE_ITEM_SIGN, nullable=False),
        sa.Column(
            "default_weight",
            sa.Numeric(4, 2),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "data_source",
            sa.String(40),
            nullable=False,
            server_default="manual",
            comment="manual / attendance / leaves / students / classroom / activity / meetings",
        ),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )

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
            "base_score",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="0",
            comment="個人基礎分（複製自 cycle.base_score 或主管覆寫）",
        ),
        sa.Column("target_enrollment", sa.Integer(), nullable=True),
        sa.Column("actual_enrollment", sa.Integer(), nullable=True),
        sa.Column(
            "hire_months_in_cycle",
            sa.Numeric(4, 2),
            nullable=False,
            server_default="6",
            comment="本學期到職月數（用於 prorate；滿學期 = 6）",
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
            "cycle_id", "employee_id", name="uq_appraisal_participant_cycle_emp"
        ),
    )
    op.create_index(
        "ix_appraisal_participant_cycle_rg",
        "appraisal_participants",
        ["cycle_id", "role_group"],
    )

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
            "item_code",
            sa.String(40),
            sa.ForeignKey(
                "appraisal_score_item_catalog.code", ondelete="RESTRICT"
            ),
            nullable=False,
        ),
        sa.Column("score_delta", sa.Numeric(5, 2), nullable=False),
        sa.Column(
            "raw_value",
            sa.Numeric(8, 2),
            nullable=True,
            comment="Excel 原始數據（如休學人數、舊生註冊率等）",
        ),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
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
            "participant_id", "item_code", name="uq_appraisal_score_item"
        ),
    )

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
            "item_score_sum",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_score",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "grade", GRADE, nullable=False, server_default="FAIL"
        ),
        sa.Column(
            "bonus_amount",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column("leave_note", sa.Text(), nullable=True),
        sa.Column(
            "status", SUMMARY_STATUS, nullable=False, server_default="DRAFT"
        ),
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

    # ── Step 5: 年終獎金表 ────────────────────────────────────────────────
    op.create_table(
        "year_end_cycles",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("academic_year", sa.Integer(), nullable=False, unique=True),
        sa.Column("status", YEAR_END_STATUS, nullable=False, server_default="DRAFT"),
        sa.Column(
            "params_snapshot",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="計算當下的 org_achievement_rate / festival_bonus_total 等參數快照",
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
    )

    op.create_table(
        "year_end_org_settings",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.BigInteger(),
            sa.ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("total_enrollment_target", sa.Integer(), nullable=False),
        sa.Column(
            "achievement_rate_first",
            sa.Numeric(5, 2),
            nullable=False,
            comment="上學期全校達成率（%）",
        ),
        sa.Column(
            "achievement_rate_second",
            sa.Numeric(5, 2),
            nullable=False,
            comment="下學期全校達成率（%）",
        ),
        sa.Column(
            "org_achievement_rate",
            sa.Numeric(5, 2),
            nullable=False,
            comment="step3 小計倍率（83.6 / 91.5 等）",
        ),
        sa.Column(
            "festival_bonus_total_amount",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
            comment="該年度節慶獎金應發總額（quota 用）",
        ),
        sa.Column(
            "org_meeting_deduction",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
            comment="機構會議扣款金額（單次）",
        ),
        sa.Column(
            "extras_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="其他自訂規則（如出勤獎金等）",
        ),
    )

    op.create_table(
        "year_end_class_targets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.BigInteger(),
            sa.ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "classroom_id",
            sa.Integer(),
            sa.ForeignKey("classrooms.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("staffing_target", sa.Integer(), nullable=False, comment="編制人數"),
        sa.Column(
            "achievement_rate_first",
            sa.Numeric(5, 2),
            nullable=False,
            comment="上學期班級達成率（%）",
        ),
        sa.Column(
            "achievement_rate_second",
            sa.Numeric(5, 2),
            nullable=False,
            comment="下學期班級達成率（%）",
        ),
        sa.Column(
            "returning_rate_first",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="0",
            comment="上學期舊生達成率（%）",
        ),
        sa.Column(
            "returning_rate_second",
            sa.Numeric(5, 2),
            nullable=False,
            server_default="0",
            comment="下學期舊生達成率（%）",
        ),
        sa.UniqueConstraint(
            "cycle_id", "classroom_id", name="uq_year_end_class_target"
        ),
    )

    op.create_table(
        "year_end_employee_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.BigInteger(),
            sa.ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "employee_id",
            sa.Integer(),
            nullable=False,
            comment="冗餘，不設 FK 避免員工刪除連動（仿 SalarySnapshot 慣例）",
        ),
        sa.Column(
            "base_salary",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
            comment="snapshot 當下的底薪",
        ),
        sa.Column(
            "festival_total",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
            comment="年度節慶獎金加總（含 2/6/9/12 月 4 個節慶）",
        ),
        sa.Column("role_group", ROLE_GROUP, nullable=False),
        sa.Column("hire_date", sa.Date(), nullable=True),
        sa.Column(
            "classroom_id",
            sa.Integer(),
            sa.ForeignKey("classrooms.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "is_resigned", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("resign_date", sa.Date(), nullable=True),
        sa.Column(
            "is_contracted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="是否已簽約（試用期未滿 / 未簽 → false 不列入年終）",
        ),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "cycle_id", "employee_id", name="uq_year_end_employee_snapshot"
        ),
    )

    op.create_table(
        "year_end_settlements",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.BigInteger(),
            sa.ForeignKey("year_end_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "snapshot_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "year_end_employee_snapshots.id", ondelete="RESTRICT"
            ),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "employee_id",
            sa.Integer(),
            nullable=False,
            comment="冗餘，方便查詢",
        ),
        # 6 層計算欄位
        sa.Column(
            "avg_performance_rate",
            sa.Numeric(6, 2),
            nullable=False,
            server_default="0",
            comment="step1：平均績效（%）",
        ),
        sa.Column(
            "gross_amount",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
            comment="step2：(base + festival_total) × 平均績效",
        ),
        sa.Column(
            "subtotal_amount",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
            comment="step3：毛額 × org_achievement_rate",
        ),
        sa.Column(
            "deduction_total",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
            comment="step4：扣項總和",
        ),
        sa.Column(
            "deduction_late",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
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
            "deduction_meeting",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "deduction_disciplinary",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "deduction_parental_leave",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "payable_subtotal",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
            comment="step5：(小計 - 扣項) × hire_months/12",
        ),
        sa.Column(
            "special_bonus_sum",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
            comment="step6：special_bonus_items 加總",
        ),
        sa.Column(
            "total_amount",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
            comment="step6：年終總額 = payable_subtotal + special_bonus_sum",
        ),
        sa.Column(
            "calc_meta",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="計算 trace（每 step 中間值、特別獎金 breakdown 等）",
        ),
        sa.Column(
            "status",
            SETTLEMENT_STATUS,
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column(
            "calculated_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "finalized_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "finalized_by",
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
    )
    op.create_index(
        "ix_year_end_settlement_cycle_emp",
        "year_end_settlements",
        ["cycle_id", "employee_id"],
    )

    op.create_table(
        "year_end_special_bonus_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "cycle_id",
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
        sa.Column(
            "period_label",
            sa.String(20),
            nullable=False,
            server_default="",
            comment="細分（如 2025-09 / 113-1 / 113-2）— 空字串表單筆",
        ),
        sa.Column(
            "amount",
            sa.Numeric(10, 2),
            nullable=False,
            comment="可為負（節慶獎金多退少補）",
        ),
        sa.Column(
            "calc_meta",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="per-type 差異欄位（class_id, lessons, rate 等）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "cycle_id",
            "employee_id",
            "bonus_type",
            "period_label",
            name="uq_year_end_special_bonus",
        ),
    )
    op.create_index(
        "ix_year_end_special_bonus_cycle_emp",
        "year_end_special_bonus_items",
        ["cycle_id", "employee_id"],
    )

    # ── Step 6: seed catalog + bonus_rates ──────────────────────────────────
    catalog_tbl = sa.table(
        "appraisal_score_item_catalog",
        sa.column("code", sa.String),
        sa.column("label", sa.String),
        sa.column("sign", sa.String),
        sa.column("default_weight", sa.Numeric),
        sa.column("data_source", sa.String),
        sa.column("display_order", sa.Integer),
        sa.column("is_active", sa.Boolean),
    )
    op.bulk_insert(
        catalog_tbl,
        [
            {
                "code": code,
                "label": label,
                "sign": sign,
                "default_weight": weight,
                "data_source": ds,
                "display_order": (idx + 1) * 10,
                "is_active": True,
            }
            for idx, (code, label, sign, weight, ds) in enumerate(
                SCORE_ITEM_CATALOG_SEED
            )
        ],
    )

    rates_tbl = sa.table(
        "appraisal_bonus_rates",
        sa.column("effective_from", sa.Date),
        sa.column("role_group", sa.String),
        sa.column("grade", sa.String),
        sa.column("base_amount", sa.Numeric),
    )
    op.bulk_insert(
        rates_tbl,
        [
            {
                "effective_from": eff,
                "role_group": rg,
                "grade": gr,
                "base_amount": amt,
            }
            for eff, rg, gr, amt in BONUS_RATES_SEED
        ],
    )


def downgrade() -> None:
    # 全表 drop（按 FK 依賴反序）
    op.drop_index(
        "ix_year_end_special_bonus_cycle_emp",
        table_name="year_end_special_bonus_items",
    )
    op.drop_table("year_end_special_bonus_items")
    op.drop_index(
        "ix_year_end_settlement_cycle_emp", table_name="year_end_settlements"
    )
    op.drop_table("year_end_settlements")
    op.drop_table("year_end_employee_snapshots")
    op.drop_table("year_end_class_targets")
    op.drop_table("year_end_org_settings")
    op.drop_table("year_end_cycles")

    op.drop_table("appraisal_bonus_rates")
    op.drop_index(
        "ix_appraisal_summary_cycle_status", table_name="appraisal_summaries"
    )
    op.drop_table("appraisal_summaries")
    op.drop_table("appraisal_score_items")
    op.drop_index(
        "ix_appraisal_participant_cycle_rg", table_name="appraisal_participants"
    )
    op.drop_table("appraisal_participants")
    op.drop_table("appraisal_score_item_catalog")
    op.drop_table("appraisal_cycles")

    # drop enum
    for enum_name in (
        "year_end_settlement_status_enum",
        "year_end_special_bonus_type_enum",
        "year_end_cycle_status_enum",
        "appraisal_score_item_sign_enum",
        "appraisal_summary_status_enum",
        "appraisal_grade_enum",
        "appraisal_role_group_enum",
        "appraisal_cycle_status_enum",
        "appraisal_semester_enum",
    ):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
