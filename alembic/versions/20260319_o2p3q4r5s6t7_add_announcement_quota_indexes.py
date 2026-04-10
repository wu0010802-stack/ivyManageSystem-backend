"""add announcement and leave_quota indexes

補齊公告與請假配額的查詢索引：
- announcements: (created_at) — portal/admin 公告按時間排序查詢
- leave_quotas: (year) — filter(year == year) 全年查詢，現有 UniqueConstraint 前導是 employee_id

Revision ID: o2p3q4r5s6t7
Revises: n2o3p4q5r6s7
Create Date: 2026-03-19 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "o2p3q4r5s6t7"
down_revision = "n2o3p4q5r6s7"
branch_labels = None
depends_on = None


def _existing_indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    # announcements: (created_at)
    # portal/admin 公告列表按建立時間排序，缺少索引導致全表掃描
    if "announcements" in tables:
        existing = _existing_indexes(bind, "announcements")
        if "ix_announcements_created_at" not in existing:
            op.create_index(
                "ix_announcements_created_at",
                "announcements",
                ["created_at"],
            )

    # leave_quotas: (year)
    # init_leave_quotas 批次初始化與年度查詢：WHERE year=? 走不到現有 (employee_id, year, leave_type) UniqueConstraint
    if "leave_quotas" in tables:
        existing = _existing_indexes(bind, "leave_quotas")
        if "ix_leave_quota_year" not in existing:
            op.create_index(
                "ix_leave_quota_year",
                "leave_quotas",
                ["year"],
            )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    if "announcements" in tables:
        existing = _existing_indexes(bind, "announcements")
        if "ix_announcements_created_at" in existing:
            op.drop_index("ix_announcements_created_at", table_name="announcements")

    if "leave_quotas" in tables:
        existing = _existing_indexes(bind, "leave_quotas")
        if "ix_leave_quota_year" in existing:
            op.drop_index("ix_leave_quota_year", table_name="leave_quotas")
