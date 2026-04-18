"""add lifecycle_status / withdrawal_date / recruitment_visit_id to students

學生紀錄追蹤大功能 Phase A：
- 新增 `lifecycle_status`（生命週期狀態 enum-like 字串）
- 新增 `withdrawal_date`（退學/轉出日期）
- 新增 `recruitment_visit_id` FK → recruitment_visits.id (ondelete SET NULL)
- 從舊 `status` 字串回填 lifecycle_status：
    「已畢業」→ graduated
    「已轉出」→ transferred
    「已退學」→ withdrawn
    「已刪除」→ withdrawn
    其餘 is_active=True → active
    其餘 is_active=False → withdrawn（保守安全態）

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-04-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "d2e3f4a5b6c7"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def _existing_cols(bind, table: str) -> set:
    return {c["name"] for c in inspect(bind).get_columns(table)}


def _existing_indexes(bind, table: str) -> set:
    return {ix["name"] for ix in inspect(bind).get_indexes(table)}


def _existing_fks(bind, table: str) -> set:
    return {fk["name"] for fk in inspect(bind).get_foreign_keys(table) if fk.get("name")}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = inspector.get_table_names()
    if "students" not in tables:
        return

    cols = _existing_cols(bind, "students")

    if "lifecycle_status" not in cols:
        op.add_column(
            "students",
            sa.Column(
                "lifecycle_status",
                sa.String(length=20),
                nullable=False,
                server_default="active",
            ),
        )

    if "withdrawal_date" not in cols:
        op.add_column(
            "students",
            sa.Column("withdrawal_date", sa.Date(), nullable=True),
        )

    if "recruitment_visit_id" not in cols:
        # recruitment_visits 可能尚未建立（測試 DB）；只在存在時建 FK
        if "recruitment_visits" in tables:
            op.add_column(
                "students",
                sa.Column(
                    "recruitment_visit_id",
                    sa.Integer,
                    sa.ForeignKey(
                        "recruitment_visits.id", ondelete="SET NULL",
                        name="fk_students_recruitment_visit",
                    ),
                    nullable=True,
                ),
            )
        else:
            op.add_column(
                "students",
                sa.Column("recruitment_visit_id", sa.Integer, nullable=True),
            )

    # 回填 lifecycle_status：依舊 status / is_active 決定
    # 只更新 server_default='active' 的既存行（全部行），用 CASE 一次性 backfill
    bind.execute(
        sa.text(
            """
            UPDATE students
            SET lifecycle_status = CASE
                WHEN status = '已畢業' THEN 'graduated'
                WHEN status = '已轉出' THEN 'transferred'
                WHEN status = '已退學' THEN 'withdrawn'
                WHEN status = '已刪除' THEN 'withdrawn'
                WHEN is_active = FALSE THEN 'withdrawn'
                ELSE 'active'
            END
            """
        )
    )

    # 回填 withdrawal_date：舊資料若 is_active=False 且有 graduation_date，
    # 但 status 非「已畢業」，則將 graduation_date 視為 withdrawal_date。
    bind.execute(
        sa.text(
            """
            UPDATE students
            SET withdrawal_date = graduation_date
            WHERE is_active = FALSE
              AND graduation_date IS NOT NULL
              AND withdrawal_date IS NULL
              AND COALESCE(status, '') <> '已畢業'
            """
        )
    )

    idx = _existing_indexes(bind, "students")
    if "ix_student_lifecycle_status" not in idx:
        op.create_index(
            "ix_student_lifecycle_status", "students", ["lifecycle_status"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "students" not in inspect(bind).get_table_names():
        return

    idx = _existing_indexes(bind, "students")
    if "ix_student_lifecycle_status" in idx:
        op.drop_index("ix_student_lifecycle_status", table_name="students")

    cols = _existing_cols(bind, "students")
    if "recruitment_visit_id" in cols:
        fks = _existing_fks(bind, "students")
        if "fk_students_recruitment_visit" in fks:
            op.drop_constraint(
                "fk_students_recruitment_visit", "students", type_="foreignkey"
            )
        op.drop_column("students", "recruitment_visit_id")
    if "withdrawal_date" in cols:
        op.drop_column("students", "withdrawal_date")
    if "lifecycle_status" in cols:
        op.drop_column("students", "lifecycle_status")
