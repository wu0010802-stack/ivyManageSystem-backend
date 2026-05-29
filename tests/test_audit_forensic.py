"""tests/test_audit_forensic.py — Ch1 AuditLog forensic readiness."""

from models.audit import AuditLog


def test_audit_log_model_has_ua_hash_and_session_id():
    """AuditLog ORM 模型必須暴露 user_agent_hash 與 session_id 兩欄。"""
    cols = {c.name for c in AuditLog.__table__.columns}
    assert "user_agent_hash" in cols, f"missing user_agent_hash, got {cols}"
    assert "session_id" in cols, f"missing session_id, got {cols}"


def test_audit_logs_table_has_session_id_index():
    """session_id 必須有 index（forensic 查詢 'find all activity of same session')."""
    indexes = list(AuditLog.__table__.indexes)
    indexed_columns = {col.name for idx in indexes for col in idx.columns}
    assert (
        "session_id" in indexed_columns
    ), f"no index covering session_id column; indexed columns: {indexed_columns}"
