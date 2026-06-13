"""activity courses/supplies 同名唯一改 partial unique（軟刪後同名重建）

Revision ID: actuq01
Revises: dbck01
Create Date: 2026-06-13

Why（K2）:
    delete_course / delete_supply 為軟刪（is_active=False），create 端點的
    重複檢查只濾 is_active=True，但 DB 的 uq_activity_course_name_term /
    uq_activity_supply_name_term 是全列 UniqueConstraint → 軟刪後同學期同名
    重建 INSERT 撞 IntegrityError → raise_safe_500，且該名稱永久不可再用
    （名稱死鎖）。

    本 migration 把兩個全列 unique constraint 改為 partial unique index
    （WHERE is_active）：軟刪列不再佔用名稱，active 列仍受 DB 層唯一兜底。
    應用層 create 的 active 重名檢查保留（友善 400）。

⚠ downgrade 限制：
    若已存在「同學期同名」的軟刪資料（或軟刪列與 active 列同名），
    downgrade 重建全列 UniqueConstraint 會失敗，須先人工 dedup
    （改名或硬刪軟刪列）後才能 downgrade。
"""

import sqlalchemy as sa
from alembic import op

revision = "actuq01"
down_revision = "dbck01"
branch_labels = None
depends_on = None

_COURSE_UQ = "uq_activity_course_name_term"
_SUPPLY_UQ = "uq_activity_supply_name_term"
_COLS = ["name", "school_year", "semester"]


def upgrade() -> None:
    op.drop_constraint(_COURSE_UQ, "activity_courses", type_="unique")
    op.create_index(
        _COURSE_UQ,
        "activity_courses",
        _COLS,
        unique=True,
        postgresql_where=sa.text("is_active = TRUE"),
        sqlite_where=sa.text("is_active = 1"),
    )

    op.drop_constraint(_SUPPLY_UQ, "activity_supplies", type_="unique")
    op.create_index(
        _SUPPLY_UQ,
        "activity_supplies",
        _COLS,
        unique=True,
        postgresql_where=sa.text("is_active = TRUE"),
        sqlite_where=sa.text("is_active = 1"),
    )


def downgrade() -> None:
    # 見 docstring：有同名軟刪資料時會失敗，須先 dedup
    op.drop_index(_COURSE_UQ, table_name="activity_courses")
    op.create_unique_constraint(_COURSE_UQ, "activity_courses", _COLS)

    op.drop_index(_SUPPLY_UQ, table_name="activity_supplies")
    op.create_unique_constraint(_SUPPLY_UQ, "activity_supplies", _COLS)
