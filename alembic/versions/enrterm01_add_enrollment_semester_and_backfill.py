"""add students.enrollment_semester + backfill visit target term & student enroll semester

Revision ID: enrterm01
Revises: mscrcptp01
Create Date: 2026-06-30
"""

from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from alembic import op

revision = "enrterm01"
down_revision = "mscrcptp01"
branch_labels = None
depends_on = None


def _date_to_term(d: date) -> tuple[int, int]:
    """純日期 → (民國學年, 學期)。mirror of utils.academic._resolve_by_date。"""
    if d.month >= 8:
        return d.year - 1911, 1
    if d.month >= 2:
        return d.year - 1 - 1911, 2
    return d.year - 1 - 1911, 1


def _roc_month_to_term(month: str | None) -> tuple[int, int] | tuple[None, None]:
    """民國月份標籤（"115.03"）→ (學年, 學期)。mirror of roc_month_to_school_term。"""
    if not month:
        return None, None
    parts = str(month).strip().split(".")
    if len(parts) < 2:
        return None, None
    try:
        roc_year = int(parts[0])
        mm = int(parts[1])
    except ValueError:
        return None, None
    if not 1 <= mm <= 12:
        return None, None
    return _date_to_term(date(roc_year + 1911, mm, 1))


def _coerce_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = str(value)[:10]
    try:
        y, m, d = s.split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. 新增欄位
    with op.batch_alter_table("students") as batch_op:
        batch_op.add_column(
            sa.Column("enrollment_semester", sa.Integer(), nullable=True)
        )

    # 2. backfill 招生訪視 target_school_year/target_semester（NULL 者用訪視月份推導）
    visit_rows = bind.execute(
        sa.text(
            "SELECT id, month FROM recruitment_visits "
            "WHERE target_school_year IS NULL OR target_semester IS NULL"
        )
    ).fetchall()
    for vid, month in visit_rows:
        sy, sem = _roc_month_to_term(month)
        if sy is None:
            continue
        bind.execute(
            sa.text(
                "UPDATE recruitment_visits "
                "SET target_school_year = COALESCE(target_school_year, :sy), "
                "    target_semester = COALESCE(target_semester, :sem) "
                "WHERE id = :id"
            ),
            {"sy": sy, "sem": sem, "id": vid},
        )

    # 3a. backfill 學生入學學期：優先取「入學」異動紀錄最早一筆的 semester
    log_rows = bind.execute(
        sa.text(
            "SELECT student_id, semester, event_date "
            "FROM student_change_logs WHERE event_type = '入學'"
        )
    ).fetchall()
    earliest: dict[int, tuple[int, date | None]] = {}
    for sid, sem, event_date in log_rows:
        d = _coerce_date(event_date)
        cur = earliest.get(sid)
        if cur is None or (d is not None and (cur[1] is None or d < cur[1])):
            earliest[sid] = (sem, d)
    for sid, (sem, _d) in earliest.items():
        if sem is None:
            continue
        bind.execute(
            sa.text(
                "UPDATE students SET enrollment_semester = :sem "
                "WHERE id = :id AND enrollment_semester IS NULL"
            ),
            {"sem": sem, "id": sid},
        )

    # 3b. 仍為 NULL 者，由 enrollment_date 推導
    rest = bind.execute(
        sa.text(
            "SELECT id, enrollment_date FROM students "
            "WHERE enrollment_semester IS NULL AND enrollment_date IS NOT NULL"
        )
    ).fetchall()
    for sid, enroll_date in rest:
        d = _coerce_date(enroll_date)
        if d is None:
            continue
        _sy, sem = _date_to_term(d)
        bind.execute(
            sa.text("UPDATE students SET enrollment_semester = :sem WHERE id = :id"),
            {"sem": sem, "id": sid},
        )


def downgrade() -> None:
    with op.batch_alter_table("students") as batch_op:
        batch_op.drop_column("enrollment_semester")
    # 註：recruitment_visits target_* 的 backfill 為資料補值，不可逆（downgrade 不還原）。
