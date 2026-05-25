"""Test audrsk01 migration: audit_logs has ack columns + index + FK

這些測試直接對 dev DB（ivymanagement）做 inspect，不需要 session fixture。
migration 必須已套用（alembic upgrade heads）才能通過。
"""

from datetime import datetime, timezone

from sqlalchemy import inspect

from models.base import get_engine, get_session
from models.audit import AuditLog

_engine = get_engine()


def test_audit_logs_has_ack_columns():
    """套完 audrsk01 migration 後 audit_logs 表應有 acknowledged_at / acknowledged_by。"""
    inspector = inspect(_engine)
    cols = {c["name"] for c in inspector.get_columns("audit_logs")}
    assert "acknowledged_at" in cols, "audit_logs 缺 acknowledged_at 欄位"
    assert "acknowledged_by" in cols, "audit_logs 缺 acknowledged_by 欄位"


def test_audit_logs_ack_index_exists():
    """ix_audit_logs_ack_created 複合 index 應存在。"""
    inspector = inspect(_engine)
    indexes = inspector.get_indexes("audit_logs")
    names = {ix["name"] for ix in indexes}
    assert (
        "ix_audit_logs_ack_created" in names
    ), f"缺 ix_audit_logs_ack_created，目前 index：{names}"


def test_audit_logs_acknowledged_by_fk_exists():
    """acknowledged_by → users.id FK 應存在。"""
    inspector = inspect(_engine)
    fks = inspector.get_foreign_keys("audit_logs")
    fk_cols = {tuple(fk["constrained_columns"]) for fk in fks}
    assert (
        "acknowledged_by",
    ) in fk_cols, f"缺 acknowledged_by FK，目前 FK 欄位：{fk_cols}"


def test_audit_log_orm_columns_mapped():
    """ORM 層 AuditLog 應有 acknowledged_at / acknowledged_by 屬性，且為 Column 型別。"""
    mapper = inspect(AuditLog)
    col_names = {col.key for col in mapper.mapper.column_attrs}
    assert "acknowledged_at" in col_names, "AuditLog ORM 缺 acknowledged_at"
    assert "acknowledged_by" in col_names, "AuditLog ORM 缺 acknowledged_by"


def test_audit_log_can_insert_with_ack_fields():
    """ORM 可以寫入含 acknowledged_at / acknowledged_by 的資料列（測試後 rollback）。"""
    session = get_session()
    try:
        row = AuditLog(
            action="DELETE",
            entity_type="employee",
            summary="刪除員工 王小明 (不可復原)",
            username="e2e_admin",
            acknowledged_at=datetime.now(timezone.utc),
            acknowledged_by=None,  # 不需 FK 真實 user，設 NULL 避免 FK violation
        )
        session.add(row)
        session.flush()  # 送 DB 但不 commit
        session.refresh(row)
        assert row.acknowledged_at is not None
        assert row.id is not None
    finally:
        session.rollback()
        session.close()
