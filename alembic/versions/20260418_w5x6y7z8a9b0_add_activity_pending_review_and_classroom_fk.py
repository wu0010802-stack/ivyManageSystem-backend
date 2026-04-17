"""add activity pending review + classroom_fk + attendance student_id

為才藝報名模組加入「靜默比對 + 待審核佇列 + 班級分組點名」所需欄位：

- activity_registrations
  - parent_phone     : 家長手機（三欄比對用，原樣儲存）
  - classroom_id     : FK → classrooms.id（匹配成功後反填；class_name 仍保留為歷史快照）
  - pending_review   : 是否待審核（比對失敗或歧義時 true）
  - match_status     : matched / pending / rejected / manual / unmatched
  - reviewed_by      : 最後處理該筆審核的員工帳號
  - reviewed_at      : 處理時間

- activity_attendances
  - student_id       : 冗餘 FK → students.id，方便點名按班級分組聯動

Backfill：
- 既有 registration.student_id 非 NULL → match_status='matched'
- 其餘 → match_status='unmatched'（pending_review 一律 false，舊資料不推入審核佇列）
- activity_registrations.classroom_id 由 students JOIN 帶入
- activity_attendances.student_id 由 registration.student_id 複製
- parent_phone 保持 NULL（不回填歷史，避免資料污染與隱私）

Revision ID: w5x6y7z8a9b0
Revises: v4w5x6y7z8a9
Create Date: 2026-04-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "w5x6y7z8a9b0"
down_revision = "v4w5x6y7z8a9"
branch_labels = None
depends_on = None


def _existing_cols(bind, table: str) -> set:
    return {c["name"] for c in inspect(bind).get_columns(table)}


def _existing_indexes(bind, table: str) -> set:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_fks(bind, table: str) -> set:
    return {
        fk["name"] for fk in inspect(bind).get_foreign_keys(table) if fk.get("name")
    }


def _existing_tables(bind) -> set:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)
    is_sqlite = bind.dialect.name == "sqlite"

    # ── activity_registrations ──────────────────────────────
    if "activity_registrations" in tables:
        cols = _existing_cols(bind, "activity_registrations")

        if "parent_phone" not in cols:
            op.add_column(
                "activity_registrations",
                sa.Column("parent_phone", sa.String(length=30), nullable=True),
            )

        if "classroom_id" not in cols:
            op.add_column(
                "activity_registrations",
                sa.Column("classroom_id", sa.Integer(), nullable=True),
            )
            if not is_sqlite and "classrooms" in tables:
                op.create_foreign_key(
                    "fk_activity_registrations_classroom_id",
                    "activity_registrations",
                    "classrooms",
                    ["classroom_id"],
                    ["id"],
                    ondelete="SET NULL",
                )

        if "pending_review" not in cols:
            op.add_column(
                "activity_registrations",
                sa.Column(
                    "pending_review",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                ),
            )

        if "match_status" not in cols:
            op.add_column(
                "activity_registrations",
                sa.Column(
                    "match_status",
                    sa.String(length=20),
                    nullable=False,
                    server_default="unmatched",
                ),
            )

        if "reviewed_by" not in cols:
            op.add_column(
                "activity_registrations",
                sa.Column("reviewed_by", sa.String(length=100), nullable=True),
            )

        if "reviewed_at" not in cols:
            op.add_column(
                "activity_registrations",
                sa.Column("reviewed_at", sa.DateTime(), nullable=True),
            )

        # Backfill match_status：既有有 student_id 的一律視為 matched
        op.execute(
            sa.text(
                "UPDATE activity_registrations "
                "SET match_status = 'matched' "
                "WHERE student_id IS NOT NULL AND (match_status IS NULL OR match_status = 'unmatched')"
            )
        )

        # Backfill classroom_id：從 students.classroom_id 帶入
        if "students" in tables and "classrooms" in tables:
            if is_sqlite:
                op.execute(
                    sa.text(
                        "UPDATE activity_registrations "
                        "SET classroom_id = (SELECT s.classroom_id FROM students s "
                        "  WHERE s.id = activity_registrations.student_id) "
                        "WHERE classroom_id IS NULL AND student_id IS NOT NULL"
                    )
                )
            else:
                op.execute(
                    sa.text(
                        "UPDATE activity_registrations AS ar "
                        "SET classroom_id = s.classroom_id "
                        "FROM students s "
                        "WHERE s.id = ar.student_id "
                        "  AND ar.classroom_id IS NULL "
                        "  AND ar.student_id IS NOT NULL"
                    )
                )

        existing_idx = _existing_indexes(bind, "activity_registrations")
        if "ix_activity_regs_pending_review" not in existing_idx:
            op.create_index(
                "ix_activity_regs_pending_review",
                "activity_registrations",
                ["pending_review", "is_active"],
            )
        if "ix_activity_regs_classroom_id" not in existing_idx:
            op.create_index(
                "ix_activity_regs_classroom_id",
                "activity_registrations",
                ["classroom_id"],
            )
        if "ix_activity_regs_match_status" not in existing_idx:
            op.create_index(
                "ix_activity_regs_match_status",
                "activity_registrations",
                ["match_status"],
            )

    # ── activity_attendances：新增 student_id 冗餘欄位 ──────
    if "activity_attendances" in tables:
        cols = _existing_cols(bind, "activity_attendances")

        if "student_id" not in cols:
            op.add_column(
                "activity_attendances",
                sa.Column("student_id", sa.Integer(), nullable=True),
            )
            if not is_sqlite and "students" in tables:
                op.create_foreign_key(
                    "fk_activity_attendances_student_id",
                    "activity_attendances",
                    "students",
                    ["student_id"],
                    ["id"],
                    ondelete="SET NULL",
                )

        # Backfill：從 registration.student_id 複製
        if "activity_registrations" in tables:
            if is_sqlite:
                op.execute(
                    sa.text(
                        "UPDATE activity_attendances "
                        "SET student_id = (SELECT r.student_id FROM activity_registrations r "
                        "  WHERE r.id = activity_attendances.registration_id) "
                        "WHERE student_id IS NULL"
                    )
                )
            else:
                op.execute(
                    sa.text(
                        "UPDATE activity_attendances AS aa "
                        "SET student_id = r.student_id "
                        "FROM activity_registrations r "
                        "WHERE r.id = aa.registration_id "
                        "  AND aa.student_id IS NULL "
                        "  AND r.student_id IS NOT NULL"
                    )
                )

        existing_idx = _existing_indexes(bind, "activity_attendances")
        if "ix_activity_attendances_student_id" not in existing_idx:
            op.create_index(
                "ix_activity_attendances_student_id",
                "activity_attendances",
                ["student_id"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)
    is_sqlite = bind.dialect.name == "sqlite"

    if "activity_attendances" in tables:
        existing_idx = _existing_indexes(bind, "activity_attendances")
        if "ix_activity_attendances_student_id" in existing_idx:
            op.drop_index(
                "ix_activity_attendances_student_id", table_name="activity_attendances"
            )
        if not is_sqlite:
            fks = _existing_fks(bind, "activity_attendances")
            if "fk_activity_attendances_student_id" in fks:
                op.drop_constraint(
                    "fk_activity_attendances_student_id",
                    "activity_attendances",
                    type_="foreignkey",
                )
        cols = _existing_cols(bind, "activity_attendances")
        if "student_id" in cols:
            op.drop_column("activity_attendances", "student_id")

    if "activity_registrations" in tables:
        existing_idx = _existing_indexes(bind, "activity_registrations")
        for idx in (
            "ix_activity_regs_match_status",
            "ix_activity_regs_classroom_id",
            "ix_activity_regs_pending_review",
        ):
            if idx in existing_idx:
                op.drop_index(idx, table_name="activity_registrations")

        if not is_sqlite:
            fks = _existing_fks(bind, "activity_registrations")
            if "fk_activity_registrations_classroom_id" in fks:
                op.drop_constraint(
                    "fk_activity_registrations_classroom_id",
                    "activity_registrations",
                    type_="foreignkey",
                )

        cols = _existing_cols(bind, "activity_registrations")
        for col in (
            "reviewed_at",
            "reviewed_by",
            "match_status",
            "pending_review",
            "classroom_id",
            "parent_phone",
        ):
            if col in cols:
                op.drop_column("activity_registrations", col)
