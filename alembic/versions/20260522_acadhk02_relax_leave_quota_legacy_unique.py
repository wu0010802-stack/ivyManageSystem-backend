"""relax uq_leave_quota to partial unique (school_year IS NULL)

Revision ID: acadhk02
Revises: acadhk01
Create Date: 2026-05-22

uq_leave_quota（無 WHERE clause）在多學年並存時與新 row 衝突。
改為 partial unique index uq_leave_quota_legacy，僅對 school_year IS NULL
的 legacy row 保持唯一，允許同 (employee_id, year, leave_type) 有多個學年 row。

upgrade:
  1. DROP CONSTRAINT/INDEX uq_leave_quota
  2. CREATE UNIQUE INDEX uq_leave_quota_legacy WHERE school_year IS NULL

downgrade（symmetric）:
  1. DROP INDEX uq_leave_quota_legacy
  2. CREATE UNIQUE INDEX/CONSTRAINT uq_leave_quota（全域，無 WHERE）
"""

from alembic import op
import sqlalchemy as sa

revision = "acadhk02"
down_revision = "acadhk01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    dialect = bind.dialect.name

    existing_idx = {i["name"] for i in insp.get_indexes("leave_quotas")}
    existing_uq = {u["name"] for u in insp.get_unique_constraints("leave_quotas")}

    # 1. 移除舊的全域 unique constraint/index
    if "uq_leave_quota" in existing_uq:
        op.drop_constraint("uq_leave_quota", "leave_quotas", type_="unique")
    elif "uq_leave_quota" in existing_idx:
        op.drop_index("uq_leave_quota", table_name="leave_quotas")

    # 2. 建立 partial unique（僅 school_year IS NULL 的 legacy row）
    if "uq_leave_quota_legacy" not in existing_idx:
        if dialect == "sqlite":
            op.create_index(
                "uq_leave_quota_legacy",
                "leave_quotas",
                ["employee_id", "year", "leave_type"],
                unique=True,
                sqlite_where=sa.text("school_year IS NULL"),
            )
        else:
            op.create_index(
                "uq_leave_quota_legacy",
                "leave_quotas",
                ["employee_id", "year", "leave_type"],
                unique=True,
                postgresql_where=sa.text("school_year IS NULL"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    dialect = bind.dialect.name

    existing_idx = {i["name"] for i in insp.get_indexes("leave_quotas")}
    existing_uq = {u["name"] for u in insp.get_unique_constraints("leave_quotas")}

    # 1. 移除 partial unique
    if "uq_leave_quota_legacy" in existing_idx:
        op.drop_index("uq_leave_quota_legacy", table_name="leave_quotas")

    # 2. 還原全域 unique constraint
    if "uq_leave_quota" not in existing_uq and "uq_leave_quota" not in existing_idx:
        if dialect == "sqlite":
            op.create_index(
                "uq_leave_quota",
                "leave_quotas",
                ["employee_id", "year", "leave_type"],
                unique=True,
            )
        else:
            op.create_unique_constraint(
                "uq_leave_quota",
                "leave_quotas",
                ["employee_id", "year", "leave_type"],
            )
