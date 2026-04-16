"""add search optimization indexes

對搜尋高頻欄位補齊 trigram / btree 索引，提升 ILIKE 查詢效能：
- employees: name, employee_id — 員工搜尋
- students: name, student_id, parent_name — 學生搜尋
- student_fee_records: student_name — 繳費紀錄搜尋
- audit_logs: username — 稽核日誌搜尋
- competitor_schools: school_name — 競爭幼兒園搜尋

Revision ID: q8r9s0t1u2v3
Revises: p7q8r9s0t1u2
Create Date: 2026-04-16 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision = "q8r9s0t1u2v3"
down_revision = "p7q8r9s0t1u2"
branch_labels = None
depends_on = None


def _existing_indexes(bind, table: str) -> set[str]:
    return {idx["name"] for idx in inspect(bind).get_indexes(table)}


def _existing_tables(bind) -> set[str]:
    return set(inspect(bind).get_table_names())


# (index_name, table, columns)
_INDEXES = [
    ("ix_employee_name", "employees", ["name"]),
    ("ix_employee_eid", "employees", ["employee_id"]),
    ("ix_student_name", "students", ["name"]),
    ("ix_student_sid", "students", ["student_id"]),
    ("ix_student_parent_name", "students", ["parent_name"]),
    ("ix_fee_record_student_name", "student_fee_records", ["student_name"]),
    ("ix_audit_log_username", "audit_logs", ["username"]),
    ("ix_competitor_school_name", "competitor_schools", ["school_name"]),
]


def upgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)

    # 嘗試啟用 pg_trgm 擴展（支援 ILIKE 索引加速）
    try:
        bind.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    except Exception:
        pass  # 權限不足時跳過，退回使用 btree 索引

    for idx_name, table, columns in _INDEXES:
        if table not in tables:
            continue
        existing = _existing_indexes(bind, table)
        if idx_name in existing:
            continue

        # 嘗試建 GIN trigram 索引（對 ILIKE 效能最好）
        if len(columns) == 1:
            try:
                bind.execute(
                    text(
                        f'CREATE INDEX "{idx_name}" ON "{table}" '
                        f'USING gin ("{columns[0]}" gin_trgm_ops)'
                    )
                )
                continue
            except Exception:
                pass  # pg_trgm 不可用，退回 btree

        op.create_index(idx_name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    tables = _existing_tables(bind)
    for idx_name, table, _columns in _INDEXES:
        if table not in tables:
            continue
        existing = _existing_indexes(bind, table)
        if idx_name in existing:
            op.drop_index(idx_name, table_name=table)
