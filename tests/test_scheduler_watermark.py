"""Tests for utils.scheduler_watermark 持久化 get/set helper。

排程器的時間游標若只存在記憶體，重啟即遺失（announcement publish
scheduler 的漏推 bug 根因）。此 helper 把游標落 DB，本測試鎖定其語意。
"""

from datetime import datetime

from utils.scheduler_watermark import get_watermark, set_watermark


def test_get_watermark_returns_none_when_unset(test_db_session):
    assert get_watermark(test_db_session, "never_set") is None


def test_set_then_get_roundtrip(test_db_session):
    ts = datetime(2026, 5, 29, 7, 0, 0)
    set_watermark(test_db_session, "announcement_publish", ts)
    test_db_session.commit()
    assert get_watermark(test_db_session, "announcement_publish") == ts


def test_set_watermark_upserts_existing_name(test_db_session):
    """同一 name 重複 set 應覆蓋（單列游標，非 append）。"""
    set_watermark(test_db_session, "wm", datetime(2026, 5, 29, 7, 0, 0))
    test_db_session.commit()
    set_watermark(test_db_session, "wm", datetime(2026, 5, 29, 8, 30, 0))
    test_db_session.commit()
    assert get_watermark(test_db_session, "wm") == datetime(2026, 5, 29, 8, 30, 0)
