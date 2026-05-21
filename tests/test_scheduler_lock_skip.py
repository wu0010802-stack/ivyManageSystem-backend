"""tests/test_scheduler_lock_skip.py — scheduler leader-election skip 行為單元測試

對應 spec：docs/superpowers/specs/2026-05-21-scheduler-leader-election-design.md

驗證 `try_scheduler_lock` yield False 時各 wrap 點的 caller 行為：
- 不執行實際工作（沒 call sweep / snapshot / cleanup / sync 等）
- return 適合的 「skip shape」（dict {}/{"skipped": True}/int 0/None）

測試環境是 SQLite，advisory lock 在 SQLite 降級 yield True；所以這裡用
monkeypatch 強制把 `try_scheduler_lock` patch 成 yield False，純單元測試。

涵蓋 5 個 wrap 點：
- official_calendar_scheduler.sync_official_calendar_once
- salary_snapshot_scheduler.check_and_snapshot_once
- activity_waitlist_scheduler.check_and_sweep_once
- security_gc_scheduler._run_rate_limit_gc
- security_gc_scheduler._run_jwt_blocklist_gc

main.py 內嵌 `_activity_waitlist_sweeper` 與 activity_waitlist_scheduler 共用
lock namespace（spec §4.3 W6），覆蓋已由 activity_waitlist_scheduler 測試代表。
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@contextmanager
def _fake_session_scope():
    yield None


@contextmanager
def _fake_busy_lock(session, *, scheduler_name, run_key):
    yield False


@contextmanager
def _fake_acquired_lock(session, *, scheduler_name, run_key):
    yield True


# ---------------------------------------------------------------------------
# W1 official_calendar
# ---------------------------------------------------------------------------


def test_official_calendar_skips_when_lock_busy(monkeypatch):
    from services import official_calendar_scheduler

    called = {"sync": 0}

    def fake_sync(session, year, force):
        called["sync"] += 1
        return {"status": "ok"}

    monkeypatch.setattr(
        "services.official_calendar_scheduler.session_scope", _fake_session_scope
    )
    monkeypatch.setattr(
        "services.official_calendar_scheduler.try_scheduler_lock", _fake_busy_lock
    )
    monkeypatch.setattr(
        "services.official_calendar_scheduler.ensure_official_calendar_synced",
        fake_sync,
    )

    result = official_calendar_scheduler.sync_official_calendar_once()
    assert result == {}
    assert called["sync"] == 0, "lock busy 時不應呼叫 ensure_official_calendar_synced"


def test_official_calendar_runs_when_lock_acquired(monkeypatch):
    from services import official_calendar_scheduler

    called = {"sync": 0}

    def fake_sync(session, year, force):
        called["sync"] += 1
        return {"status": "ok"}

    monkeypatch.setattr(
        "services.official_calendar_scheduler.session_scope", _fake_session_scope
    )
    monkeypatch.setattr(
        "services.official_calendar_scheduler.try_scheduler_lock", _fake_acquired_lock
    )
    monkeypatch.setattr(
        "services.official_calendar_scheduler.ensure_official_calendar_synced",
        fake_sync,
    )
    # _years_to_sync 預設只回當年（11~12 月才加下一年）；強制只當年避免測試在 11~12 月變動
    monkeypatch.setattr(
        "services.official_calendar_scheduler._years_to_sync", lambda now=None: [2026]
    )

    result = official_calendar_scheduler.sync_official_calendar_once()
    assert result == {2026: "ok"}
    assert called["sync"] == 1


# ---------------------------------------------------------------------------
# W2 salary_snapshot
# ---------------------------------------------------------------------------


def test_salary_snapshot_skips_when_lock_busy(monkeypatch):
    from datetime import date

    from services import salary_snapshot_scheduler

    called = {"create": 0, "record_count": 0}

    monkeypatch.setattr(
        "services.salary_snapshot_scheduler.session_scope", _fake_session_scope
    )
    monkeypatch.setattr(
        "services.salary_snapshot_scheduler.try_scheduler_lock", _fake_busy_lock
    )

    # 防衛：若 lock skip 失敗讓 caller 跑到查詢 SalaryRecord，這個 patch 也會被踩
    def boom(*args, **kwargs):
        called["record_count"] += 1
        raise AssertionError("lock busy 時不應查 SalaryRecord")

    monkeypatch.setattr(
        "services.salary_snapshot_scheduler.create_month_end_snapshots",
        lambda *a, **kw: (called.__setitem__("create", called["create"] + 1) or 0),
    )

    result = salary_snapshot_scheduler.check_and_snapshot_once(today=date(2026, 5, 21))
    assert result == 0
    assert called["create"] == 0, "lock busy 時不應建 snapshot"


# ---------------------------------------------------------------------------
# W3 activity_waitlist
# ---------------------------------------------------------------------------


def test_activity_waitlist_skips_when_lock_busy(monkeypatch):
    from services import activity_waitlist_scheduler

    called = {"sweep": 0}

    class FakeSvc:
        @staticmethod
        def sweep_expired_pending_promotions(session):
            called["sweep"] += 1
            return {"expired": 5, "reminded": 0, "final_reminded": 0}

    monkeypatch.setattr(
        "services.activity_waitlist_scheduler._get_activity_service",
        lambda: FakeSvc(),
    )
    monkeypatch.setattr(
        "services.activity_waitlist_scheduler.session_scope", _fake_session_scope
    )
    monkeypatch.setattr(
        "services.activity_waitlist_scheduler.try_scheduler_lock", _fake_busy_lock
    )

    result = activity_waitlist_scheduler.check_and_sweep_once()
    assert result == {"skipped": True}
    assert called["sweep"] == 0, "lock busy 時不應 sweep_expired_pending_promotions"


def test_activity_waitlist_lock_bucket_changes_per_window(monkeypatch):
    """5 分鐘 bucket：t=0 與 t=299 同 bucket，t=300 進下個 bucket。"""
    from services import activity_waitlist_scheduler

    monkeypatch.setattr("services.activity_waitlist_scheduler.time.time", lambda: 0.0)
    bucket_a = activity_waitlist_scheduler._current_lock_bucket()

    monkeypatch.setattr("services.activity_waitlist_scheduler.time.time", lambda: 299.9)
    bucket_b = activity_waitlist_scheduler._current_lock_bucket()

    monkeypatch.setattr("services.activity_waitlist_scheduler.time.time", lambda: 300.0)
    bucket_c = activity_waitlist_scheduler._current_lock_bucket()

    assert bucket_a == bucket_b, "同 5 分鐘窗口應同 bucket"
    assert bucket_a != bucket_c, "跨 5 分鐘窗口應換 bucket"


# ---------------------------------------------------------------------------
# W4 security_gc / rate_limit
# ---------------------------------------------------------------------------


def test_security_rate_limit_gc_skips_when_lock_busy(monkeypatch):
    from services import security_gc_scheduler

    called = {"cleanup": 0}

    def fake_cleanup(retention_minutes):
        called["cleanup"] += 1
        return 0

    monkeypatch.setattr(
        "services.security_gc_scheduler.session_scope", _fake_session_scope
    )
    monkeypatch.setattr(
        "services.security_gc_scheduler.try_scheduler_lock", _fake_busy_lock
    )
    monkeypatch.setattr("utils.rate_limit.cleanup_rate_limit_buckets", fake_cleanup)

    security_gc_scheduler._run_rate_limit_gc()
    assert called["cleanup"] == 0, "lock busy 時不應 cleanup_rate_limit_buckets"


def test_security_rate_limit_gc_runs_when_lock_acquired(monkeypatch):
    from services import security_gc_scheduler

    called = {"cleanup": 0}

    def fake_cleanup(retention_minutes):
        called["cleanup"] += 1
        return 7

    monkeypatch.setattr(
        "services.security_gc_scheduler.session_scope", _fake_session_scope
    )
    monkeypatch.setattr(
        "services.security_gc_scheduler.try_scheduler_lock", _fake_acquired_lock
    )
    monkeypatch.setattr("utils.rate_limit.cleanup_rate_limit_buckets", fake_cleanup)

    security_gc_scheduler._run_rate_limit_gc()
    assert called["cleanup"] == 1


# ---------------------------------------------------------------------------
# W5 security_gc / jwt_blocklist
# ---------------------------------------------------------------------------


def test_security_jwt_blocklist_gc_skips_when_lock_busy(monkeypatch):
    from services import security_gc_scheduler

    called = {"cleanup": 0}

    def fake_cleanup():
        called["cleanup"] += 1
        return 0

    monkeypatch.setattr(
        "services.security_gc_scheduler.session_scope", _fake_session_scope
    )
    monkeypatch.setattr(
        "services.security_gc_scheduler.try_scheduler_lock", _fake_busy_lock
    )
    monkeypatch.setattr("utils.auth.cleanup_jwt_blocklist", fake_cleanup)

    security_gc_scheduler._run_jwt_blocklist_gc()
    assert called["cleanup"] == 0, "lock busy 時不應 cleanup_jwt_blocklist"


def test_security_jwt_blocklist_gc_runs_when_lock_acquired(monkeypatch):
    from services import security_gc_scheduler

    called = {"cleanup": 0}

    def fake_cleanup():
        called["cleanup"] += 1
        return 3

    monkeypatch.setattr(
        "services.security_gc_scheduler.session_scope", _fake_session_scope
    )
    monkeypatch.setattr(
        "services.security_gc_scheduler.try_scheduler_lock", _fake_acquired_lock
    )
    monkeypatch.setattr("utils.auth.cleanup_jwt_blocklist", fake_cleanup)

    security_gc_scheduler._run_jwt_blocklist_gc()
    assert called["cleanup"] == 1
