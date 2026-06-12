"""零風險約束補強：5 條 enum 值域 CHECK + 7 欄 NOT NULL

Revision ID: dbck01
Revises: dbidx01
Create Date: 2026-06-12

Why（DB 優化健檢 2026-06-12 SCH-6 / SCH-8 零風險子集，
diagnosis：.scratch/db-optimize-2026-06-12/schema-findings.md）:

CHECK（值域全部抄自程式碼 enum / 常數 / API 白名單，dev DB 0 違規）：
    - ck_student_attendances_status：API 白名單 VALID_STATUSES 五值（中文）
    - ck_fee_records_status：unpaid/partial/paid（寫入站點僅此三值）
    - ck_students_lifecycle_status：LIFECYCLE_* 七值
      （變更必經 services/student_lifecycle.transition()）
    - ck_apr_type：payment/refund
    - ck_apr_amount_positive：amount > 0（欄位註解「永遠為正整數」）

NOT NULL（現有資料 0 NULL + ORM default 雙保險，加約束不改任何行為）：
    - attendances.status（default normal）
    - salary_records.is_finalized / gross_salary / total_deduction / net_salary
      （default False / 0，引擎 _fill_salary_record 必寫）
    - leave_records.leave_hours / is_deductible（default 8 / True）

models/ 已同步：__table_args__ 加同名 CheckConstraint、7 欄 nullable=False
（SQLite 測試 DB 由 metadata 建表，兩邊一致）。

SQLite：無法 ALTER 加 CHECK / SET NOT NULL，且測試 DB 由 metadata 直接
建表已含約束，本 migration 僅在 PostgreSQL 執行。
downgrade：drop 5 CHECK、7 欄恢復 nullable。

註（2026-06-13 修正）：原版含 ck_attendances_status（AttendanceStatus 六值），
但 attendances.status 實為開放複合值域——utils/attendance_calc.py 與
api/attendance/upload.py 會以 '+' 串接（如 'late+missing_out'、
'late+early_leave'），列舉式 CHECK 會讓補打卡核准等寫入路徑 500，
全套 pytest 抓到後撤除。該欄 NOT NULL 保留。
"""

import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger(__name__)

revision = "dbck01"
down_revision = "dbidx01"
branch_labels = None
depends_on = None


# (constraint 名, 表名, CHECK 條件 SQL)
_CHECKS = [
    (
        "ck_student_attendances_status",
        "student_attendances",
        "status IN ('出席','缺席','病假','事假','遲到')",
    ),
    (
        "ck_fee_records_status",
        "student_fee_records",
        "status IN ('unpaid','partial','paid')",
    ),
    (
        "ck_students_lifecycle_status",
        "students",
        "lifecycle_status IN ('prospect','enrolled','active','on_leave',"
        "'transferred','withdrawn','graduated')",
    ),
    (
        "ck_apr_type",
        "activity_payment_records",
        "type IN ('payment','refund')",
    ),
    (
        "ck_apr_amount_positive",
        "activity_payment_records",
        "amount > 0",
    ),
]

# (表名, 欄位名, 型別)——型別供 alter_column existing_type 用
_NOT_NULL_COLUMNS = [
    ("attendances", "status", sa.String(20)),
    ("salary_records", "is_finalized", sa.Boolean()),
    ("salary_records", "gross_salary", sa.Numeric(12, 2)),
    ("salary_records", "total_deduction", sa.Numeric(12, 2)),
    ("salary_records", "net_salary", sa.Numeric(12, 2)),
    ("leave_records", "leave_hours", sa.Float()),
    ("leave_records", "is_deductible", sa.Boolean()),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        logger.info("非 PostgreSQL（%s），跳過約束補強", bind.dialect.name)
        return

    for name, table, condition in _CHECKS:
        op.create_check_constraint(name, table, condition)
        logger.info("已加 CHECK %s ON %s", name, table)

    for table, column, coltype in _NOT_NULL_COLUMNS:
        op.alter_column(table, column, existing_type=coltype, nullable=False)
        logger.info("已設 NOT NULL %s.%s", table, column)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table, column, coltype in _NOT_NULL_COLUMNS:
        op.alter_column(table, column, existing_type=coltype, nullable=True)

    for name, table, _condition in _CHECKS:
        op.drop_constraint(name, table, type_="check")
