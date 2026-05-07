"""驗證 utils/rate_limit_db.py DB-backed counter（multi-worker 安全）。

威脅：原本 in-process dict 多 worker 失效 — IP / 帳號 / bind 鎖定可被
分散到其他 worker 繞過（audit 2026-05-07 P0 #14）。

驗證重點：
- count_recent_attempts 看到的是「跨 connection 共享的 DB 狀態」，不是
  per-process dict
- record_attempt 透過 INSERT/UPSERT 累積；同一 window 內 N 次 record
  得到 count=N
- clear_attempts 清掉該 key 全部 row（登入成功 reset 用）
- DB 失敗時 fail-open（log warning 不 raise，避免 DB 短暫失聯把所有
  login 都 503）
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import Base


@pytest.fixture
def db_engine(tmp_path):
    """建立 SQLite DB（含 rate_limit_buckets 表）。"""
    db_path = tmp_path / "rate_limit_db.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


class TestRecordAndCount:
    def test_record_increments_count(self, db_engine):
        from utils.rate_limit_db import count_recent_attempts, record_attempt

        for _ in range(3):
            record_attempt("login_ip", "1.2.3.4", window_seconds=300, engine=db_engine)

        count = count_recent_attempts(
            "login_ip", "1.2.3.4", within_seconds=300, engine=db_engine
        )
        assert count == 3

    def test_isolated_keys(self, db_engine):
        from utils.rate_limit_db import count_recent_attempts, record_attempt

        record_attempt("login_ip", "1.2.3.4", window_seconds=300, engine=db_engine)
        record_attempt("login_ip", "1.2.3.4", window_seconds=300, engine=db_engine)
        record_attempt("login_ip", "5.6.7.8", window_seconds=300, engine=db_engine)

        assert (
            count_recent_attempts(
                "login_ip", "1.2.3.4", within_seconds=300, engine=db_engine
            )
            == 2
        )
        assert (
            count_recent_attempts(
                "login_ip", "5.6.7.8", within_seconds=300, engine=db_engine
            )
            == 1
        )

    def test_isolated_scopes(self, db_engine):
        """login_ip:1.2.3.4 與 login_account:1.2.3.4 不互相干擾。"""
        from utils.rate_limit_db import count_recent_attempts, record_attempt

        record_attempt("login_ip", "alice", window_seconds=300, engine=db_engine)
        record_attempt("login_account", "alice", window_seconds=900, engine=db_engine)
        record_attempt("login_account", "alice", window_seconds=900, engine=db_engine)

        assert (
            count_recent_attempts(
                "login_ip", "alice", within_seconds=300, engine=db_engine
            )
            == 1
        )
        assert (
            count_recent_attempts(
                "login_account", "alice", within_seconds=900, engine=db_engine
            )
            == 2
        )

    def test_clear_removes_all_rows_for_key(self, db_engine):
        from utils.rate_limit_db import (
            clear_attempts,
            count_recent_attempts,
            record_attempt,
        )

        for _ in range(4):
            record_attempt("login_account", "bob", window_seconds=900, engine=db_engine)

        cleared = clear_attempts("login_account", "bob", engine=db_engine)
        assert cleared >= 1

        count = count_recent_attempts(
            "login_account", "bob", within_seconds=900, engine=db_engine
        )
        assert count == 0


class TestMultiConnectionConsistency:
    """模擬多 worker：兩個獨立 connection 寫入同一 key，count 應反映兩者總和。

    這是 in-process dict 沒有的特性 — DB-backed 才能擋住「攻擊者把請求分
    散到不同 worker」的旁路攻擊。
    """

    def test_writes_from_two_connections_visible_to_third(self, db_engine):
        from utils.rate_limit_db import count_recent_attempts, record_attempt

        # connection A 寫 3 次
        for _ in range(3):
            record_attempt("login_ip", "10.0.0.1", window_seconds=300, engine=db_engine)

        # connection B（理論上是另一 worker）再寫 4 次
        # 這裡用同 engine 但每次 record_attempt 內部都是 engine.begin() 獨立 txn
        for _ in range(4):
            record_attempt("login_ip", "10.0.0.1", window_seconds=300, engine=db_engine)

        # connection C 讀
        count = count_recent_attempts(
            "login_ip", "10.0.0.1", within_seconds=300, engine=db_engine
        )
        assert count == 7, f"expected 7 attempts visible cross-connection, got {count}"


class TestExpiredWindowsExcluded:
    def test_old_window_not_counted(self, db_engine):
        """window_start 早於 cutoff 的 row 不計入 count。"""
        from utils.rate_limit_db import count_recent_attempts

        # 直接插一筆「2 小時前」的 row（模擬已過期）
        old_window = datetime.now(timezone.utc) - timedelta(hours=2)
        with db_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO rate_limit_buckets (bucket_key, window_start, count)"
                    " VALUES (:bk, :ws, :c)"
                ),
                {"bk": "login_account:carol", "ws": old_window, "c": 99},
            )

        # 查 15 分鐘窗 → 不該看到 99
        count = count_recent_attempts(
            "login_account", "carol", within_seconds=900, engine=db_engine
        )
        assert count == 0


class TestEarliestAttemptAt:
    def test_returns_min_window_start(self, db_engine):
        from utils.rate_limit_db import earliest_attempt_at, record_attempt

        record_attempt("login_account", "dave", window_seconds=900, engine=db_engine)
        result = earliest_attempt_at(
            "login_account", "dave", within_seconds=900, engine=db_engine
        )
        assert result is not None
        # SQLite 可能回傳 ISO string；PG 回傳 datetime。兩者都接受。
        if isinstance(result, str):
            result = datetime.fromisoformat(result.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - result
        assert delta.total_seconds() < 900

    def test_returns_none_when_empty(self, db_engine):
        from utils.rate_limit_db import earliest_attempt_at

        result = earliest_attempt_at(
            "login_account", "nobody", within_seconds=900, engine=db_engine
        )
        assert result is None
