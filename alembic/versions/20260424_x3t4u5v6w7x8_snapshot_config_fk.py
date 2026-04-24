"""salary_snapshots: 加 bonus_config_id / attendance_policy_id FK

與 SalaryRecord 同步保存計算時使用的設定版本，使快照可獨立稽核
（不需回查現有 SalaryRecord 即可證明當時套用的獎金設定 / 考勤政策）。

Revision ID: x3t4u5v6w7x8
Revises: w2s3t4u5v6w7
Create Date: 2026-04-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "x3t4u5v6w7x8"
down_revision = "w2s3t4u5v6w7"
branch_labels = None
depends_on = None


_TABLE = "salary_snapshots"


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if _TABLE not in insp.get_table_names():
        return
    existing_cols = {c["name"] for c in insp.get_columns(_TABLE)}

    with op.batch_alter_table(_TABLE) as batch_op:
        if "bonus_config_id" not in existing_cols:
            batch_op.add_column(sa.Column("bonus_config_id", sa.Integer, nullable=True))
        if "attendance_policy_id" not in existing_cols:
            batch_op.add_column(
                sa.Column("attendance_policy_id", sa.Integer, nullable=True)
            )

    # SQLite 不支援 ALTER TABLE ADD CONSTRAINT；PostgreSQL 才加 FK。
    if bind.dialect.name == "postgresql":
        existing_fks = {fk["name"] for fk in insp.get_foreign_keys(_TABLE)}
        if "fk_salary_snapshot_bonus_config_id" not in existing_fks:
            op.create_foreign_key(
                "fk_salary_snapshot_bonus_config_id",
                _TABLE,
                "bonus_configs",
                ["bonus_config_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if "fk_salary_snapshot_attendance_policy_id" not in existing_fks:
            op.create_foreign_key(
                "fk_salary_snapshot_attendance_policy_id",
                _TABLE,
                "attendance_policies",
                ["attendance_policy_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if _TABLE not in insp.get_table_names():
        return

    if bind.dialect.name == "postgresql":
        existing_fks = {fk["name"] for fk in insp.get_foreign_keys(_TABLE)}
        if "fk_salary_snapshot_bonus_config_id" in existing_fks:
            op.drop_constraint(
                "fk_salary_snapshot_bonus_config_id", _TABLE, type_="foreignkey"
            )
        if "fk_salary_snapshot_attendance_policy_id" in existing_fks:
            op.drop_constraint(
                "fk_salary_snapshot_attendance_policy_id", _TABLE, type_="foreignkey"
            )

    existing_cols = {c["name"] for c in insp.get_columns(_TABLE)}
    with op.batch_alter_table(_TABLE) as batch_op:
        if "attendance_policy_id" in existing_cols:
            batch_op.drop_column("attendance_policy_id")
        if "bonus_config_id" in existing_cols:
            batch_op.drop_column("bonus_config_id")
