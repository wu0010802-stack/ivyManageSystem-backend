"""students.recruitment_visit_id partial unique index（R4-1 重複轉換兜底）

Revision ID: recvisuq01
Revises: yebnd01
Create Date: 2026-06-06

Why（R4-1）:
    convert_recruitment_to_student 的「已轉化」檢查為無鎖 .first()，且 legacy
    POST /records/{id}/convert 路徑無 row lock（只 funnel 路徑有 with_for_update）。
    並發轉換同一 visit → 兩請求各查到 existing=None → 各建一個 Student，DB 無唯一鍵
    兜底 → 重複 Student → recruitment_intake_plan 的 enrolled count 灌倍、名額算錯。

    本 migration 加 partial unique index：一個 recruitment_visit 最多對應一個
    Student；recruitment_visit_id IS NULL（未透過 convert 建立者）允許多筆。
    第二筆並發轉換的 flush 會撞此 index → service 已改 catch IntegrityError 轉成
    友善 RecruitmentConversionError（非 500）。

⚠ 前置：套用前 prod students 表的 recruitment_visit_id 不可已有重複非 NULL 值
    （若 bug 已在 prod 觸發過，須先 dedup），否則 CREATE UNIQUE INDEX 會失敗。
    PG / SQLite 皆支援 partial unique index。
"""

import sqlalchemy as sa
from alembic import op

revision = "recvisuq01"
down_revision = "yebnd01"
branch_labels = None
depends_on = None

_INDEX = "uq_students_recruitment_visit_id"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "students",
        ["recruitment_visit_id"],
        unique=True,
        postgresql_where=sa.text("recruitment_visit_id IS NOT NULL"),
        sqlite_where=sa.text("recruitment_visit_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="students")
