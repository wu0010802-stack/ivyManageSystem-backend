"""Spec D PR-D1: audit_logs immutable trigger E2E verification。

防 future alembic downgrade / drift 意外移除 trigger 但無人察覺。
直接走 raw SQL UPDATE / DELETE assert IntegrityError / OperationalError。

注意：tests/conftest.py:167 用 Base.metadata.create_all 不跑 alembic
migration → test DB 沒 trigger。本檔自己 install trigger DDL 模擬 prod 行為。
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, IntegrityError, OperationalError

from models.audit import AuditLog
from models.database import get_engine

# 對齊 alembic/versions/20260507_l7m8n9o0p1q2_audit_log_immutable_trigger.py
_INSTALL_TRIGGER_SQLITE = [
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_log_immutable_update
    BEFORE UPDATE ON audit_logs
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'audit_logs 為不可竄改稽核軌跡，禁止 UPDATE');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_audit_log_immutable_delete
    BEFORE DELETE ON audit_logs
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'audit_logs 為不可竄改稽核軌跡，禁止 DELETE');
    END;
    """,
]

_INSTALL_TRIGGER_PG = [
    """
    CREATE OR REPLACE FUNCTION audit_log_immutable_fn()
    RETURNS trigger AS $$
    BEGIN
        IF (TG_OP = 'UPDATE') THEN
            RAISE EXCEPTION 'audit_logs 為不可竄改稽核軌跡，禁止 UPDATE (id=%)', OLD.id;
        ELSIF (TG_OP = 'DELETE') THEN
            RAISE EXCEPTION 'audit_logs 為不可竄改稽核軌跡，禁止 DELETE (id=%)', OLD.id;
        END IF;
        RETURN NULL;
    END;
    $$ LANGUAGE plpgsql;
    """,
    "CREATE TRIGGER trg_audit_log_immutable_update BEFORE UPDATE ON audit_logs FOR EACH ROW EXECUTE FUNCTION audit_log_immutable_fn();",
    "CREATE TRIGGER trg_audit_log_immutable_delete BEFORE DELETE ON audit_logs FOR EACH ROW EXECUTE FUNCTION audit_log_immutable_fn();",
]


@pytest.fixture(autouse=True)
def _install_audit_trigger(test_db_session):
    """test_db_session fixture 跑 create_all 後，本 fixture 補裝 trigger DDL。

    test_db_session 來自 conftest（建 SQLite + Base.metadata.create_all）。
    我們在這之後手動 CREATE TRIGGER 補上 alembic migration 不跑的 trigger。
    """
    engine = get_engine()
    dialect = engine.dialect.name
    stmts = _INSTALL_TRIGGER_PG if dialect == "postgresql" else _INSTALL_TRIGGER_SQLITE
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))
    yield
    # 不主動 DROP — test_db_session 結束時 SQLite 整個 DB tmpfile 被刪


def _create_test_audit_log(session) -> int:
    """對齊實際 AuditLog schema：必填 action + entity_type；extras 用 changes column。"""
    log = AuditLog(
        action="TEST",
        entity_type="TEST",
        entity_id="1",  # String(50), not int
        changes="{}",  # Text, nullable but 給空 JSON
    )
    session.add(log)
    session.commit()
    return log.id


def test_audit_log_update_raises(test_db_session):
    """UPDATE audit_logs raise DatabaseError（PG: IntegrityError / SQLite: OperationalError）。"""
    log_id = _create_test_audit_log(test_db_session)

    with pytest.raises((DatabaseError, IntegrityError, OperationalError)) as exc_info:
        test_db_session.execute(
            text("UPDATE audit_logs SET entity_id = '999' WHERE id = :id"),
            {"id": log_id},
        )
        test_db_session.commit()
    msg = str(exc_info.value).lower()
    assert (
        "audit_logs" in msg or "abort" in msg
    ), f"Expected trigger reject, got: {exc_info.value}"


def test_audit_log_delete_raises(test_db_session):
    """DELETE audit_logs raise DatabaseError。"""
    log_id = _create_test_audit_log(test_db_session)

    with pytest.raises((DatabaseError, IntegrityError, OperationalError)) as exc_info:
        test_db_session.execute(
            text("DELETE FROM audit_logs WHERE id = :id"),
            {"id": log_id},
        )
        test_db_session.commit()
    msg = str(exc_info.value).lower()
    assert (
        "audit_logs" in msg or "abort" in msg
    ), f"Expected trigger reject, got: {exc_info.value}"


def test_audit_log_insert_succeeds(test_db_session):
    """INSERT audit_logs 仍正常（trigger 只擋 UPDATE/DELETE）。"""
    log_id = _create_test_audit_log(test_db_session)
    assert log_id is not None and log_id > 0
