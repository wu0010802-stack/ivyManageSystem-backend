"""驗證 audit_logs 不可竄改 trigger 在 SQLite 下擋 UPDATE / DELETE。

PG 版採同樣語意（plpgsql RAISE EXCEPTION），無法在純單元測試中跑；本測試
僅覆蓋 SQLite 版（測試用）。實際 prod 跑 alembic upgrade head 即套 PG 版
（trg_audit_log_immutable_update / _delete + audit_log_immutable_fn）。

Refs: 邏輯漏洞 audit 2026-05-07 P0 #12（user 拍板採 DB trigger 方案）；
migration: alembic/versions/20260507_l7m8n9o0p1q2_audit_log_immutable_trigger.py
"""

import os
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import AuditLog, Base


@pytest.fixture
def db_with_trigger(tmp_path):
    """建立 SQLite DB → 建表 → 套 trigger（migration 的 SQLite 版）。"""
    db_path = tmp_path / "audit_log_immutable.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)

    # 套 migration 的 SQLite trigger（與 alembic upgrade 等價）
    with engine.begin() as conn:
        conn.exec_driver_sql("""
            CREATE TRIGGER trg_audit_log_immutable_update
            BEFORE UPDATE ON audit_logs
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'audit_logs 為不可竄改稽核軌跡，禁止 UPDATE');
            END;
            """)
        conn.exec_driver_sql("""
            CREATE TRIGGER trg_audit_log_immutable_delete
            BEFORE DELETE ON audit_logs
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'audit_logs 為不可竄改稽核軌跡，禁止 DELETE');
            END;
            """)

    Sess = sessionmaker(bind=engine)
    yield engine, Sess
    engine.dispose()


def _seed_audit_log(session, **overrides):
    fields = dict(
        user_id=1,
        username="admin",
        action="UPDATE",
        entity_type="employee",
        entity_id="42",
        summary="test entry",
        ip_address="127.0.0.1",
        created_at=datetime(2026, 5, 7, 12, 0, 0),
    )
    fields.update(overrides)
    log = AuditLog(**fields)
    session.add(log)
    session.flush()
    return log


class TestAuditLogImmutableTrigger:
    def test_insert_allowed(self, db_with_trigger):
        """INSERT 不受 trigger 影響（這是稽核唯一進入路徑）。"""
        engine, Sess = db_with_trigger
        with Sess() as s:
            log = _seed_audit_log(s)
            s.commit()
            assert log.id is not None

    def test_update_via_orm_blocked(self, db_with_trigger):
        engine, Sess = db_with_trigger
        with Sess() as s:
            log = _seed_audit_log(s)
            s.commit()
            log_id = log.id

        with Sess() as s:
            log = s.get(AuditLog, log_id)
            log.summary = "tampered"
            with pytest.raises((IntegrityError, OperationalError)) as exc:
                s.commit()
            # SQLite RAISE(ABORT) 訊息應出現在 exception 內
            assert "audit_logs" in str(exc.value)

    def test_delete_via_orm_blocked(self, db_with_trigger):
        engine, Sess = db_with_trigger
        with Sess() as s:
            log = _seed_audit_log(s)
            s.commit()
            log_id = log.id

        with Sess() as s:
            log = s.get(AuditLog, log_id)
            s.delete(log)
            with pytest.raises((IntegrityError, OperationalError)) as exc:
                s.commit()
            assert "audit_logs" in str(exc.value)

    def test_update_via_raw_sql_blocked(self, db_with_trigger):
        """走 raw SQL 仍被擋（防 admin 透過 psql 直接 UPDATE 的攻擊路徑）。"""
        engine, Sess = db_with_trigger
        with Sess() as s:
            log = _seed_audit_log(s)
            s.commit()
            log_id = log.id

        with engine.begin() as conn:
            with pytest.raises((IntegrityError, OperationalError)) as exc:
                conn.exec_driver_sql(
                    f"UPDATE audit_logs SET summary='hacked' WHERE id={log_id}"
                )
            assert "audit_logs" in str(exc.value)

    def test_delete_via_raw_sql_blocked(self, db_with_trigger):
        engine, Sess = db_with_trigger
        with Sess() as s:
            log = _seed_audit_log(s)
            s.commit()
            log_id = log.id

        with engine.begin() as conn:
            with pytest.raises((IntegrityError, OperationalError)) as exc:
                conn.exec_driver_sql(f"DELETE FROM audit_logs WHERE id={log_id}")
            assert "audit_logs" in str(exc.value)

    def test_insert_after_failed_update_still_works(self, db_with_trigger):
        """失敗的 UPDATE rollback 後仍可繼續 INSERT 新 log。"""
        engine, Sess = db_with_trigger
        with Sess() as s:
            log = _seed_audit_log(s, summary="第一筆")
            s.commit()
            log_id = log.id

        with Sess() as s:
            target = s.get(AuditLog, log_id)
            target.summary = "篡改"
            with pytest.raises((IntegrityError, OperationalError)):
                s.commit()
            s.rollback()

        with Sess() as s:
            new_log = _seed_audit_log(s, summary="第二筆")
            s.commit()
            assert new_log.id != log_id

        with Sess() as s:
            rows = s.query(AuditLog).order_by(AuditLog.id).all()
            assert len(rows) == 2
            assert [r.summary for r in rows] == ["第一筆", "第二筆"]
