"""索引整理：補 2 個缺索引 FK + 刪 12 個結構冗餘索引

Revision ID: dbidx01
Revises: cascfx01
Create Date: 2026-06-12

Why（DB 優化健檢 2026-06-12，diagnosis：.scratch/db-optimize-2026-06-12/index-findings.md）:

新增 2 個索引：
    - ix_staff_refresh_parent_token (staff_refresh_tokens.parent_token_id)
      自參照 FK ON DELETE SET NULL 無覆蓋索引——security GC 每日 bulk DELETE
      （services/security_gc_scheduler.py）每刪一筆 token 觸發一次反向全表
      seq scan（O(刪除數 × 全表)）。EXPLAIN 已實證 seq scan。
    - ix_recruitment_event_log_student_id (recruitment_event_log.student_id)
      14 個缺索引 FK 中唯一有真實 WHERE 過濾的
      （services/student_records_timeline.py:444,450）；另 students 硬刪
      SET NULL 反向查找。

刪除 12 個冗餘索引（已逐一複核：皆 btree、非 unique、非 partial、無
expression，且被同表另一索引完全覆蓋——btree 前綴規則）：
    1. ix_leave_status_date — 與 ix_leave_status_start_date 100% 重複
       （models/leave.py 同欄位 Index 宣告兩次，本批已刪重複宣告）
    2. ix_activity_pos_daily_close_history_close_date
       ⊂ ix_pos_close_history_date_unlocked
    3. ix_fk_attendance_employee ⊂ uq_attendance_employee_date
       （來自 20260416_t2u3v4w5x6y7，models 無宣告，DB-only）
    4. ix_dsr_requests_user_id ⊂ ix_dsr_user_type
    5. ix_enrollment_cert_year ⊂ uq_enrollment_cert_year_seq
    6. ix_medical_access_log_student_id ⊂ ix_mal_student_field_time
    7. ix_monthly_fixed_costs_year ⊂ ix_monthly_fixed_costs_year_month
    8. ix_monthly_fixed_costs_year_month ⊂ uq_monthly_fixed_costs_period_cat
    9. ix_notif_pref_user_event ⊂ uq_notif_pref_triple（models 無宣告，DB-only）
    10. ix_fk_overtime_employee ⊂ ix_overtime_emp_status
        （來自 20260416_t2u3v4w5x6y7，models 無宣告，DB-only）
    11. ix_parent_consent_log_user_id ⊂ ix_pcl_user_scope_time
    12. ix_staff_refresh_tokens_user_id ⊂ ix_staff_refresh_user_family

models/ 同步：有宣告者（index=True / Index(...)）已同批移除宣告，
否則 autogenerate 會把索引加回來。

SQLite（pytest）：測試 DB 由 models metadata 直接建表，本 migration
僅在 PostgreSQL 執行。downgrade：照原始 pg_indexes.indexdef 重建 12 個
被刪索引、刪 2 個新索引。
"""

import logging

from alembic import op

logger = logging.getLogger(__name__)

revision = "dbidx01"
down_revision = "cascfx01"
branch_labels = None
depends_on = None


# (索引名, 表名, 欄位 tuple)——欄位定義照 dev DB pg_indexes.indexdef 原樣
_DROPPED_INDEXES = [
    ("ix_leave_status_date", "leave_records", ("status", "start_date")),
    (
        "ix_activity_pos_daily_close_history_close_date",
        "activity_pos_daily_close_history",
        ("close_date",),
    ),
    ("ix_fk_attendance_employee", "attendances", ("employee_id",)),
    ("ix_dsr_requests_user_id", "dsr_requests", ("user_id",)),
    ("ix_enrollment_cert_year", "enrollment_certificates", ("year",)),
    ("ix_medical_access_log_student_id", "medical_access_log", ("student_id",)),
    ("ix_monthly_fixed_costs_year", "monthly_fixed_costs", ("year",)),
    ("ix_monthly_fixed_costs_year_month", "monthly_fixed_costs", ("year", "month")),
    ("ix_notif_pref_user_event", "notification_preferences", ("user_id", "event_type")),
    ("ix_fk_overtime_employee", "overtime_records", ("employee_id",)),
    ("ix_parent_consent_log_user_id", "parent_consent_log", ("user_id",)),
    ("ix_staff_refresh_tokens_user_id", "staff_refresh_tokens", ("user_id",)),
]

_NEW_INDEXES = [
    ("ix_staff_refresh_parent_token", "staff_refresh_tokens", ("parent_token_id",)),
    (
        "ix_recruitment_event_log_student_id",
        "recruitment_event_log",
        ("student_id",),
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        logger.info("非 PostgreSQL（%s），跳過索引整理", bind.dialect.name)
        return

    for name, table, columns in _NEW_INDEXES:
        op.create_index(name, table, list(columns))
        logger.info("已建立索引 %s ON %s %s", name, table, columns)

    for name, table, _columns in _DROPPED_INDEXES:
        op.drop_index(name, table_name=table)
        logger.info("已刪除冗餘索引 %s（%s）", name, table)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for name, table, columns in _DROPPED_INDEXES:
        op.create_index(name, table, list(columns))

    for name, table, _columns in _NEW_INDEXES:
        op.drop_index(name, table_name=table)
