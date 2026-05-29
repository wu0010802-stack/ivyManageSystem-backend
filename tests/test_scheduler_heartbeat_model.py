"""SchedulerHeartbeat ORM model 單元測試。

驗證：
- 建立 row 後 query 取得對應欄位
- last_success_at / last_failure_at / last_error_message 預設 NULL
- consecutive_failures / last_rows_processed 預設 0
- updated_at server_default + onupdate 行為由 DB 處理
"""

from __future__ import annotations

from models.scheduler_heartbeat import SchedulerHeartbeat


def test_heartbeat_create_minimal(test_db_session):
    hb = SchedulerHeartbeat(
        scheduler_name="alpha",
        expected_interval_seconds=300,
    )
    test_db_session.add(hb)
    test_db_session.commit()

    row = (
        test_db_session.query(SchedulerHeartbeat)
        .filter_by(scheduler_name="alpha")
        .one()
    )
    assert row.expected_interval_seconds == 300
    assert row.last_success_at is None
    assert row.last_failure_at is None
    assert row.consecutive_failures == 0
    assert row.last_error_message is None
    assert row.last_rows_processed == 0


def test_heartbeat_update_fields(test_db_session):
    from datetime import datetime, timezone

    hb = SchedulerHeartbeat(
        scheduler_name="beta",
        expected_interval_seconds=86400,
    )
    test_db_session.add(hb)
    test_db_session.commit()

    now = datetime.now(timezone.utc)
    hb.last_success_at = now
    hb.last_rows_processed = 12
    test_db_session.commit()

    row = (
        test_db_session.query(SchedulerHeartbeat).filter_by(scheduler_name="beta").one()
    )
    assert row.last_success_at is not None
    assert row.last_rows_processed == 12
