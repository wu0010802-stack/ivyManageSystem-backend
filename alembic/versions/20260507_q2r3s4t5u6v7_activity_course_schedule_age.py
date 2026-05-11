"""activity_courses 加適齡月齡 + 結構化時段欄位（Phase 3 — 前台不適齡/衝堂檢核）

加 5 個 nullable 欄位給家長公開報名頁做「警告但不阻擋」的檢核：

1. `min_age_months` (Int nullable) — 建議最小月齡；家長生日換算月齡 < 此值時前台顯示
   黃色「適合 X 歲以上」chip，但仍允許勾選送出（業主既有政策：不限制家長自助佔位）。
2. `max_age_months` (Int nullable) — 建議最大月齡；同上。
3. `meeting_weekday` (Int nullable, 0=Mon, 6=Sun) — 上課星期（Python weekday() 慣例）。
4. `meeting_start_time` (Time nullable) — 上課起始時刻。
5. `meeting_end_time` (Time nullable) — 上課結束時刻。

衝堂檢測邏輯（前台 computed）：兩堂課的 weekday 相同且時段重疊時，在課程卡顯示
紅色 chip「與 OOO 衝堂」，不阻擋勾選；管理者在後台也能審視。

Why: 既有 `frequency` 是 free-text（且 /public/courses 端點目前 hardcode 回 ""），
無法可靠程式判斷。新增結構化欄位後前台才能精準告警。

向後相容：所有欄位 nullable，既有課程資料不受影響；前台缺資料時 advisory helper
回 null（不顯示警告 chip）。

Revision ID: q2r3s4t5u6v7
Revises: o0p1q2r3s4t5
Create Date: 2026-05-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "q2r3s4t5u6v7"
down_revision = "o0p1q2r3s4t5"
branch_labels = None
depends_on = None


_NEW_COLS = (
    (
        "min_age_months",
        sa.Column(
            "min_age_months",
            sa.Integer(),
            nullable=True,
            comment="建議最小月齡；前台不適齡時顯示警告 chip（不阻擋送出）",
        ),
    ),
    (
        "max_age_months",
        sa.Column(
            "max_age_months",
            sa.Integer(),
            nullable=True,
            comment="建議最大月齡；前台不適齡時顯示警告 chip（不阻擋送出）",
        ),
    ),
    (
        "meeting_weekday",
        sa.Column(
            "meeting_weekday",
            sa.Integer(),
            nullable=True,
            comment="上課星期（0=Mon, 6=Sun，遵循 Python weekday() 慣例）",
        ),
    ),
    (
        "meeting_start_time",
        sa.Column(
            "meeting_start_time",
            sa.Time(),
            nullable=True,
            comment="上課起始時刻；用於衝堂檢測",
        ),
    ),
    (
        "meeting_end_time",
        sa.Column(
            "meeting_end_time",
            sa.Time(),
            nullable=True,
            comment="上課結束時刻；用於衝堂檢測",
        ),
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "activity_courses" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("activity_courses")}
    for name, col in _NEW_COLS:
        if name not in existing:
            op.add_column("activity_courses", col)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "activity_courses" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("activity_courses")}
    for name, _col in reversed(_NEW_COLS):
        if name in existing:
            op.drop_column("activity_courses", name)
