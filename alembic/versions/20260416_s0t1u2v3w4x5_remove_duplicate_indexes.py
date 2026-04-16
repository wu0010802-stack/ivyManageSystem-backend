"""remove duplicate and unused indexes

移除 16 組完全重複的索引（同欄位雙索引）以及 4 個從未使用的 recruitment_visits 索引。
重複索引浪費磁碟空間且拖慢每次 INSERT/UPDATE/DELETE 操作。

Revision ID: s0t1u2v3w4x5
Revises: r9s0t1u2v3w4
Create Date: 2026-04-16 00:00:00.000000
"""

from alembic import op
from sqlalchemy import inspect

revision = "s0t1u2v3w4x5"
down_revision = "r9s0t1u2v3w4"
branch_labels = None
depends_on = None

# (index_to_drop, table_name)
_DUPLICATE_INDEXES = [
    # 同欄位重複索引（保留另一個）
    ("ix_overtime_approved_date", "overtime_records"),
    ("ix_salary_bonus_config_id", "salary_records"),
    ("ix_salary_attendance_policy_id", "salary_records"),
    ("ix_leave_approved_start_date", "leave_records"),
    ("ix_employee_classroom_id", "employees"),
    ("ix_employee_job_title_id", "employees"),
    # PK 已有 btree unique index，額外的 btree(id) 完全冗餘
    ("ix_recruitment_visits_id", "recruitment_visits"),
    ("ix_recruitment_geocode_cache_id", "recruitment_geocode_cache"),
    ("ix_recruitment_area_insight_cache_id", "recruitment_area_insight_cache"),
    ("ix_recruitment_sync_states_id", "recruitment_sync_states"),
    ("ix_recruitment_competitors_id", "recruitment_competitors"),
    ("ix_recruitment_campus_settings_id", "recruitment_campus_settings"),
    ("ix_recruitment_periods_id", "recruitment_periods"),
    ("ix_recruitment_ivykids_records_id", "recruitment_ivykids_records"),
    ("ix_recruitment_months_id", "recruitment_months"),
    ("ix_job_titles_id", "job_titles"),
]

# recruitment_visits 從未被使用的索引
_UNUSED_INDEXES = [
    ("ix_rv_has_deposit", "recruitment_visits"),
    ("ix_rv_has_deposit_grade", "recruitment_visits"),
    ("ix_rv_no_deposit_reason", "recruitment_visits"),
    ("ix_recruitment_visits_district", "recruitment_visits"),
]


def _existing_indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    for idx_name, table in _DUPLICATE_INDEXES + _UNUSED_INDEXES:
        if table not in tables:
            continue
        existing = _existing_indexes(bind, table)
        if idx_name in existing:
            op.drop_index(idx_name, table_name=table)


def downgrade() -> None:
    # 重複索引不需要還原——它們本來就不該存在
    pass
