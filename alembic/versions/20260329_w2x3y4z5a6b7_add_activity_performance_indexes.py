"""add activity performance indexes

新增課後才藝系統效能索引：
- activity_registrations: (is_active, created_at) — 報名列表排序查詢
- registration_supplies: (registration_id) — 用品關聯查詢
- registration_courses: (course_id, registration_id, status) — 課程容量確認

Revision ID: w2x3y4z5a6b7
Revises: v1w2x3y4z5a6
Create Date: 2026-03-29 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "w2x3y4z5a6b7"
down_revision = "v1w2x3y4z5a6"
branch_labels = None
depends_on = None


def _existing_indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    # activity_registrations: (is_active, created_at)
    if "activity_registrations" in tables:
        existing = _existing_indexes(bind, "activity_registrations")
        if "ix_activity_regs_active_created" not in existing:
            op.create_index(
                "ix_activity_regs_active_created",
                "activity_registrations",
                ["is_active", "created_at"],
            )

    # registration_supplies: (registration_id)
    if "registration_supplies" in tables:
        existing = _existing_indexes(bind, "registration_supplies")
        if "ix_reg_supply_reg" not in existing:
            op.create_index(
                "ix_reg_supply_reg",
                "registration_supplies",
                ["registration_id"],
            )

    # registration_courses: (course_id, registration_id, status)
    if "registration_courses" in tables:
        existing = _existing_indexes(bind, "registration_courses")
        if "ix_reg_courses_course_reg" not in existing:
            op.create_index(
                "ix_reg_courses_course_reg",
                "registration_courses",
                ["course_id", "registration_id", "status"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if "activity_registrations" in tables:
        existing = _existing_indexes(bind, "activity_registrations")
        if "ix_activity_regs_active_created" in existing:
            op.drop_index("ix_activity_regs_active_created", table_name="activity_registrations")

    if "registration_supplies" in tables:
        existing = _existing_indexes(bind, "registration_supplies")
        if "ix_reg_supply_reg" in existing:
            op.drop_index("ix_reg_supply_reg", table_name="registration_supplies")

    if "registration_courses" in tables:
        existing = _existing_indexes(bind, "registration_courses")
        if "ix_reg_courses_course_reg" in existing:
            op.drop_index("ix_reg_courses_course_reg", table_name="registration_courses")
