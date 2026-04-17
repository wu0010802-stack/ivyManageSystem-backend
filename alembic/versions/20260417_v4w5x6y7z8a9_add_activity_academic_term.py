"""add activity academic term fields

為課後才藝模組加入學期制：
- activity_courses / activity_supplies / activity_registrations：加 school_year + semester
- activity_registrations：加 student_id FK → students.id（nullable，SET NULL）
- 把所有既有資料回填為當前學期
- activity_courses.name 原本 unique → 改為 (name, school_year, semester) 複合唯一
- 加上索引：(school_year, semester) 與 student_id

Revision ID: v4w5x6y7z8a9
Revises: u3v4w5x6y7z8
Create Date: 2026-04-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "v4w5x6y7z8a9"
down_revision = "u3v4w5x6y7z8"
branch_labels = None
depends_on = None


def _existing_cols(bind, table: str) -> set:
    return {c["name"] for c in inspect(bind).get_columns(table)}


def _existing_indexes(bind, table: str) -> set:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_uniques(bind, table: str) -> set:
    return {uq["name"] for uq in inspect(bind).get_unique_constraints(table)}


def _existing_fks(bind, table: str) -> set:
    return {
        fk["name"] for fk in inspect(bind).get_foreign_keys(table) if fk.get("name")
    }


def _existing_tables(bind) -> set:
    return set(inspect(bind).get_table_names())


def _resolve_current_term():
    """計算民國年當前學期：8月後→當年上學期，2-7月→前年下學期，1月→前年上學期"""
    from datetime import date

    today = date.today()
    if today.month >= 8:
        return today.year - 1911, 1
    if today.month >= 2:
        return today.year - 1 - 1911, 2
    return today.year - 1 - 1911, 1


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)
    year, sem = _resolve_current_term()

    # ── activity_courses：加欄位 + 改唯一約束 ──
    if "activity_courses" in tables:
        cols = _existing_cols(bind, "activity_courses")
        if "school_year" not in cols:
            op.add_column(
                "activity_courses",
                sa.Column("school_year", sa.Integer(), nullable=True),
            )
        if "semester" not in cols:
            op.add_column(
                "activity_courses", sa.Column("semester", sa.Integer(), nullable=True)
            )

        # 回填當前學期到所有既有課程
        op.execute(
            sa.text(
                "UPDATE activity_courses SET school_year = :y, semester = :s "
                "WHERE school_year IS NULL OR semester IS NULL"
            ).bindparams(y=year, s=sem)
        )

        # 舊 unique(name) 改為 (name, school_year, semester)
        uqs = _existing_uniques(bind, "activity_courses")
        # SQLite 對既有 unique 以 index 形式存在，檢查 indexes
        existing_idx = _existing_indexes(bind, "activity_courses")
        dialect = bind.dialect.name

        if dialect != "sqlite":
            # 嘗試移除舊的單欄 unique（不同 DB 約束名不同）
            for uq_name in list(uqs):
                if "name" in uq_name.lower() and "term" not in uq_name.lower():
                    try:
                        op.drop_constraint(uq_name, "activity_courses", type_="unique")
                    except Exception:
                        pass
            if "uq_activity_course_name_term" not in uqs:
                op.create_unique_constraint(
                    "uq_activity_course_name_term",
                    "activity_courses",
                    ["name", "school_year", "semester"],
                )
        else:
            # SQLite：建立新 index 即可，舊的 unique 約束保留容忍（測試已新表）
            if "ix_activity_courses_name_term" not in existing_idx:
                op.create_index(
                    "ix_activity_courses_name_term",
                    "activity_courses",
                    ["name", "school_year", "semester"],
                    unique=True,
                )

        # 常用索引
        if "ix_activity_courses_term" not in existing_idx:
            op.create_index(
                "ix_activity_courses_term",
                "activity_courses",
                ["school_year", "semester"],
            )

    # ── activity_supplies：加欄位 + 改唯一約束 ──
    if "activity_supplies" in tables:
        cols = _existing_cols(bind, "activity_supplies")
        if "school_year" not in cols:
            op.add_column(
                "activity_supplies",
                sa.Column("school_year", sa.Integer(), nullable=True),
            )
        if "semester" not in cols:
            op.add_column(
                "activity_supplies", sa.Column("semester", sa.Integer(), nullable=True)
            )
        op.execute(
            sa.text(
                "UPDATE activity_supplies SET school_year = :y, semester = :s "
                "WHERE school_year IS NULL OR semester IS NULL"
            ).bindparams(y=year, s=sem)
        )
        existing_idx = _existing_indexes(bind, "activity_supplies")
        if "ix_activity_supplies_term" not in existing_idx:
            op.create_index(
                "ix_activity_supplies_term",
                "activity_supplies",
                ["school_year", "semester"],
            )
        # SQLite 保留舊 unique；其他 DB 改成複合
        if bind.dialect.name != "sqlite":
            uqs = _existing_uniques(bind, "activity_supplies")
            for uq_name in list(uqs):
                if "name" in uq_name.lower() and "term" not in uq_name.lower():
                    try:
                        op.drop_constraint(uq_name, "activity_supplies", type_="unique")
                    except Exception:
                        pass
            if "uq_activity_supply_name_term" not in uqs:
                op.create_unique_constraint(
                    "uq_activity_supply_name_term",
                    "activity_supplies",
                    ["name", "school_year", "semester"],
                )

    # ── activity_registrations：加欄位 + student_id FK ──
    if "activity_registrations" in tables:
        cols = _existing_cols(bind, "activity_registrations")
        if "school_year" not in cols:
            op.add_column(
                "activity_registrations",
                sa.Column("school_year", sa.Integer(), nullable=True),
            )
        if "semester" not in cols:
            op.add_column(
                "activity_registrations",
                sa.Column("semester", sa.Integer(), nullable=True),
            )
        if "student_id" not in cols:
            op.add_column(
                "activity_registrations",
                sa.Column("student_id", sa.Integer(), nullable=True),
            )
            # FK 僅在非 SQLite 且 students 存在時建立
            if bind.dialect.name != "sqlite" and "students" in tables:
                op.create_foreign_key(
                    "fk_activity_registrations_student_id",
                    "activity_registrations",
                    "students",
                    ["student_id"],
                    ["id"],
                    ondelete="SET NULL",
                )

        # 回填當前學期
        op.execute(
            sa.text(
                "UPDATE activity_registrations SET school_year = :y, semester = :s "
                "WHERE school_year IS NULL OR semester IS NULL"
            ).bindparams(y=year, s=sem)
        )

        # 回填 student_id：依 (name, birthday) 匹配啟用中學生
        if "students" in tables:
            if bind.dialect.name == "sqlite":
                op.execute(
                    sa.text(
                        "UPDATE activity_registrations "
                        "SET student_id = (SELECT s.id FROM students s "
                        "  WHERE s.name = activity_registrations.student_name "
                        "  AND s.birthday = activity_registrations.birthday "
                        "  AND s.is_active = 1 LIMIT 1) "
                        "WHERE student_id IS NULL"
                    )
                )
            else:
                # PostgreSQL 日期比較：Student.birthday 是 DATE，activity.birthday 是字串 "YYYY-MM-DD"
                op.execute(
                    sa.text(
                        "UPDATE activity_registrations AS ar "
                        "SET student_id = s.id "
                        "FROM students s "
                        "WHERE s.name = ar.student_name "
                        "  AND to_char(s.birthday, 'YYYY-MM-DD') = ar.birthday "
                        "  AND s.is_active = TRUE "
                        "  AND ar.student_id IS NULL"
                    )
                )

        existing_idx = _existing_indexes(bind, "activity_registrations")
        if "ix_activity_regs_term" not in existing_idx:
            op.create_index(
                "ix_activity_regs_term",
                "activity_registrations",
                ["school_year", "semester"],
            )
        if "ix_activity_regs_student_id" not in existing_idx:
            op.create_index(
                "ix_activity_regs_student_id",
                "activity_registrations",
                ["student_id"],
            )
        # 複合：(student_id, school_year, semester) 加速「某生某學期的所有報名」
        if "ix_activity_regs_student_term" not in existing_idx:
            op.create_index(
                "ix_activity_regs_student_term",
                "activity_registrations",
                ["student_id", "school_year", "semester"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if "activity_registrations" in tables:
        existing_idx = _existing_indexes(bind, "activity_registrations")
        for idx in (
            "ix_activity_regs_student_term",
            "ix_activity_regs_student_id",
            "ix_activity_regs_term",
        ):
            if idx in existing_idx:
                op.drop_index(idx, table_name="activity_registrations")
        if bind.dialect.name != "sqlite":
            fks = _existing_fks(bind, "activity_registrations")
            if "fk_activity_registrations_student_id" in fks:
                op.drop_constraint(
                    "fk_activity_registrations_student_id",
                    "activity_registrations",
                    type_="foreignkey",
                )
        cols = _existing_cols(bind, "activity_registrations")
        for col in ("student_id", "semester", "school_year"):
            if col in cols:
                op.drop_column("activity_registrations", col)

    if "activity_supplies" in tables:
        existing_idx = _existing_indexes(bind, "activity_supplies")
        if "ix_activity_supplies_term" in existing_idx:
            op.drop_index("ix_activity_supplies_term", table_name="activity_supplies")
        cols = _existing_cols(bind, "activity_supplies")
        for col in ("semester", "school_year"):
            if col in cols:
                op.drop_column("activity_supplies", col)

    if "activity_courses" in tables:
        existing_idx = _existing_indexes(bind, "activity_courses")
        for idx in ("ix_activity_courses_term", "ix_activity_courses_name_term"):
            if idx in existing_idx:
                op.drop_index(idx, table_name="activity_courses")
        if bind.dialect.name != "sqlite":
            uqs = _existing_uniques(bind, "activity_courses")
            if "uq_activity_course_name_term" in uqs:
                op.drop_constraint(
                    "uq_activity_course_name_term", "activity_courses", type_="unique"
                )
        cols = _existing_cols(bind, "activity_courses")
        for col in ("semester", "school_year"):
            if col in cols:
                op.drop_column("activity_courses", col)
