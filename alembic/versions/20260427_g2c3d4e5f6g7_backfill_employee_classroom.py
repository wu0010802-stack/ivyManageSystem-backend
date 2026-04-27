"""backfill Employee.classroom_id from current-term Classroom assignments

修正 #1：班級頁面更新教師時未同步 Employee.classroom_id，導致薪資引擎、
匯出、員工 API 對「目前帶班教師」全面失準。本遷移做一次性回填：

  對每位 active 員工，檢視當前學期的 active classrooms 中其是否被指派為
  head/assistant/art_teacher，若有則寫回 Employee.classroom_id（優先序：
  head > assistant > art > id 較小）。沒有任何指派則保持原值（避免不知情
  地清空舊資料）。

Why 不直接全清空再回填：可能有其他流程（例如手動匯入）寫過 classroom_id；
此遷移聚焦於把「真的有班但 classroom_id 為空或不一致」的人補正。

Revision ID: g2c3d4e5f6g7
Revises: f1b2c3d4e5f6
Create Date: 2026-04-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "g2c3d4e5f6g7"
down_revision = "f1b2c3d4e5f6"
branch_labels = None
depends_on = None


def _resolve_current_term(today):
    """與 utils.academic.resolve_current_academic_term 邏輯一致（民國年）。

    內嵌避免遷移在不同 alembic context 下 import application code 失敗。
    """
    if today.month >= 8:
        return today.year - 1911, 1
    if today.month >= 2:
        return today.year - 1 - 1911, 2
    return today.year - 1 - 1911, 1


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "classrooms" not in tables or "employees" not in tables:
        return

    import datetime

    school_year, semester = _resolve_current_term(datetime.date.today())

    rows = bind.execute(
        sa.text("""
            SELECT id, head_teacher_id, assistant_teacher_id, art_teacher_id
            FROM classrooms
            WHERE is_active = TRUE
              AND school_year = :sy
              AND semester = :sem
            ORDER BY id ASC
            """),
        {"sy": school_year, "sem": semester},
    ).fetchall()

    primary: dict[int, tuple[int, int]] = {}  # employee_id → (classroom_id, role)

    def _record(emp_id, classroom_id, role: int) -> None:
        # role: 1=head, 2=assistant, 3=art；數字越小優先序越高
        existing = primary.get(emp_id)
        if existing is None:
            primary[emp_id] = (classroom_id, role)
        else:
            _, prev_role = existing
            if role < prev_role:
                primary[emp_id] = (classroom_id, role)

    for cls_id, head_id, assistant_id, art_id in rows:
        if head_id:
            _record(head_id, cls_id, 1)
        if assistant_id:
            _record(assistant_id, cls_id, 2)
        if art_id:
            _record(art_id, cls_id, 3)

    if not primary:
        return

    for emp_id, (cls_id, _role) in primary.items():
        bind.execute(
            sa.text("""
                UPDATE employees SET classroom_id = :cid
                WHERE id = :eid AND (classroom_id IS NULL OR classroom_id != :cid)
                """),
            {"cid": cls_id, "eid": emp_id},
        )


def downgrade() -> None:
    # 一次性資料修補無 downgrade（無法還原原本的 classroom_id 值）
    pass
