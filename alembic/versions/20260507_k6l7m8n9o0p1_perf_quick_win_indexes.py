"""perf quick-win indexes（audit Top 2）

從 backend perf audit 2026-05-07 Top 2 衍生：補 5 個關鍵 index，
全部 additive、idempotent，不影響既有資料。

- ix_employee_hire_date: _active_employees_in_month_filter 走的 hire_date
  目前無 index，每次薪資/festival/dashboard 都 seq scan（A.P0.4）
- ix_apr_payment_date_voided: 7+ 個 POS 端點過濾 payment_date，
  partial index 排除 voided 紀錄壓縮體積（D.P0.1）
- ix_audit_entity_created / ix_audit_user_created: 取代既有
  (entity_type, entity_id) 與 (user_id) 單欄 index 之外，補 created_at DESC
  讓 audit list 排序走 index（H.P0.2）
- ix_change_logs_term_event_date: student_change_logs 列表 ORDER BY
  event_date DESC 但只有單欄 index，加複合 index（H.P1）

Revision ID: k6l7m8n9o0p1
Revises: j5k6l7m8n9o0
Create Date: 2026-05-07
"""

from alembic import op
from sqlalchemy import inspect

revision = "k6l7m8n9o0p1"
down_revision = "j5k6l7m8n9o0"
branch_labels = None
depends_on = None


_INDEXES = [
    (
        "ix_employee_hire_date",
        "employees",
        'CREATE INDEX IF NOT EXISTS "ix_employee_hire_date" ON employees (hire_date)',
    ),
    (
        "ix_apr_payment_date_voided",
        "activity_payment_records",
        'CREATE INDEX IF NOT EXISTS "ix_apr_payment_date_voided" '
        "ON activity_payment_records (payment_date, voided_at) "
        "WHERE voided_at IS NULL",
    ),
    (
        "ix_audit_entity_created",
        "audit_logs",
        'CREATE INDEX IF NOT EXISTS "ix_audit_entity_created" '
        "ON audit_logs (entity_type, entity_id, created_at DESC)",
    ),
    (
        "ix_audit_user_created",
        "audit_logs",
        'CREATE INDEX IF NOT EXISTS "ix_audit_user_created" '
        "ON audit_logs (user_id, created_at DESC)",
    ),
    (
        "ix_change_logs_term_event_date",
        "student_change_logs",
        'CREATE INDEX IF NOT EXISTS "ix_change_logs_term_event_date" '
        "ON student_change_logs (school_year, semester, event_date DESC)",
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for _index_name, table, sql_create in _INDEXES:
        if table not in existing_tables:
            continue
        op.execute(sql_create)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for index_name, table, _sql_create in _INDEXES:
        if table not in existing_tables:
            continue
        op.execute(f'DROP INDEX IF EXISTS "{index_name}"')
