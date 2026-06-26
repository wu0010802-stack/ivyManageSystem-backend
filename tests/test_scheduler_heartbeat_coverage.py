"""排程器 heartbeat 覆蓋驗證：漏帶 expected_interval_seconds 的 4 個 iteration。

機制：``scheduler_iteration(...)`` 未帶 ``expected_interval_seconds`` 就不寫
``scheduler_heartbeats`` row → ``/health/schedulers`` 對該 job 全盲
（process restart 後完全看不到該 scheduler 的最近成功時間）。

本檔鎖住 4 個曾漏網的 call site，要求各自 iteration 跑完必須留下 heartbeat row：

- ``pii_retention_employee``（services/pii_retention_scheduler.py loop 內第二段）
- ``data_quality``（services/data_quality_scheduler.py loop 內）
- ``security_staff_refresh_gc``（services/security_gc_scheduler.py）
- ``security_recruitment_geocode_cache_gc``（services/security_gc_scheduler.py）
"""

from __future__ import annotations

import asyncio

import pytest

from models.scheduler_heartbeat import SchedulerHeartbeat
from utils import scheduler_observability as so


@pytest.fixture(autouse=True)
def _reset_metrics():
    so.reset_for_tests()
    yield
    so.reset_for_tests()


def _get_heartbeat(session, name: str) -> SchedulerHeartbeat | None:
    session.expire_all()
    return (
        session.query(SchedulerHeartbeat).filter_by(scheduler_name=name).one_or_none()
    )


# ── security_gc_scheduler：兩個 GC 包裝器 ────────────────────────────────────


def test_staff_refresh_gc_persists_heartbeat(test_db_session):
    from services import security_gc_scheduler as sched

    sched._run_staff_refresh_gc()

    row = _get_heartbeat(test_db_session, "security_staff_refresh_gc")
    assert row is not None, "security_staff_refresh_gc 未寫 heartbeat（/health 全盲）"
    assert row.last_success_at is not None
    assert row.expected_interval_seconds == sched._STAFF_REFRESH_GC_INTERVAL_SEC


def test_recruitment_geocode_cache_gc_persists_heartbeat(test_db_session):
    from services import security_gc_scheduler as sched

    sched._run_recruitment_geocode_cache_gc()

    row = _get_heartbeat(test_db_session, "security_recruitment_geocode_cache_gc")
    assert (
        row is not None
    ), "security_recruitment_geocode_cache_gc 未寫 heartbeat（/health 全盲）"
    assert row.last_success_at is not None
    assert (
        row.expected_interval_seconds
        == sched._RECRUITMENT_GEOCODE_CACHE_GC_INTERVAL_SEC
    )


# ── pii_retention_scheduler：loop 內第二段 employee GC ───────────────────────


def test_pii_retention_employee_iteration_persists_heartbeat(
    test_db_session, monkeypatch
):
    from services import pii_retention_scheduler as sched

    stop_event = asyncio.Event()
    monkeypatch.setattr(sched, "_INITIAL_DELAY_SEC", 0)
    monkeypatch.setattr(sched, "_run_pii_retention_gc", lambda: None)

    def _fake_employee_gc():
        stop_event.set()  # 跑完第一輪 iteration 即停 loop

    monkeypatch.setattr(sched, "_run_employee_pii_retention_gc", _fake_employee_gc)

    asyncio.run(
        asyncio.wait_for(sched.run_pii_retention_scheduler(stop_event), timeout=10)
    )

    row = _get_heartbeat(test_db_session, "pii_retention_employee")
    assert row is not None, "pii_retention_employee 未寫 heartbeat（/health 全盲）"
    assert row.last_success_at is not None
    assert row.expected_interval_seconds == sched._GC_INTERVAL_SEC


# ── data_quality_scheduler：loop 內 daily iteration ──────────────────────────


def test_data_quality_iteration_persists_heartbeat(test_db_session, monkeypatch):
    from services import data_quality_scheduler as sched

    stop_event = asyncio.Event()
    monkeypatch.setattr(sched, "scheduler_enabled", lambda: True)
    monkeypatch.setattr(sched, "should_run_data_quality", lambda *args, **kwargs: True)

    def _fake_run_once():
        stop_event.set()  # 跑完第一輪 iteration 即停 loop
        return {"detected": 0, "new_open": 0, "ran_at": "test"}

    monkeypatch.setattr(sched, "run_data_quality_once", _fake_run_once)

    asyncio.run(
        asyncio.wait_for(sched.run_data_quality_scheduler(stop_event), timeout=10)
    )

    row = _get_heartbeat(test_db_session, "data_quality")
    assert row is not None, "data_quality 未寫 heartbeat（/health 全盲）"
    assert row.last_success_at is not None
    # 每日 03:00 跑一次 → expected interval 為一天（非 60s 巡檢週期，
    # 否則 /health/schedulers 對日級 job 永遠誤判 lagging）
    assert row.expected_interval_seconds == 24 * 60 * 60


# ── P2（深度 QA 2026-06-13）：日級/年級 job 的 heartbeat expected_interval ─────
# 機制：scheduler_iteration(expected_interval_seconds=巡檢週期 60/300s) 對「每日/每年
# 才跑一次」的 job，heartbeat 在跑完那次後數分鐘即 lag>2×巡檢週期 → /health/schedulers
# 永久 503 degraded（淹沒真故障、外部 uptime 誤警）。日級 job 應傳一天；年級 job
# 應每巡檢 tick 都寫 heartbeat（liveness），與 data_quality 對齊。


def test_finance_reconciliation_iteration_persists_daily_heartbeat(
    test_db_session, monkeypatch
):
    from services import finance_reconciliation_scheduler as sched

    stop_event = asyncio.Event()
    monkeypatch.setattr(sched, "should_run_reconciliation", lambda *a, **k: True)

    def _fake_run(*a, **k):
        stop_event.set()
        return {"mismatch_count": 0}

    monkeypatch.setattr(sched, "run_finance_reconciliation", _fake_run)

    asyncio.run(
        asyncio.wait_for(
            sched.run_finance_reconciliation_scheduler(stop_event), timeout=10
        )
    )

    row = _get_heartbeat(test_db_session, "finance_reconciliation")
    assert row is not None, "finance_reconciliation 未寫 heartbeat"
    assert row.last_success_at is not None
    # 每日 02:00 跑一次 → interval 為一天（非 60s 巡檢週期）
    assert row.expected_interval_seconds == 24 * 60 * 60


def test_medication_reminder_iteration_persists_daily_heartbeat(
    test_db_session, monkeypatch
):
    from datetime import timedelta

    from services import medication_reminder_scheduler as sched

    stop_event = asyncio.Event()
    # 讓 now >= target（target 設過去）→ 觸發 iteration
    monkeypatch.setattr(
        sched,
        "_today_target_dt",
        lambda today: sched._now_taipei() - timedelta(hours=1),
    )

    def _fake_run(effective_date=None):
        stop_event.set()
        return {"order_count": 0}

    monkeypatch.setattr(sched, "run_medication_reminder", _fake_run)

    asyncio.run(
        asyncio.wait_for(sched.medication_reminder_loop(stop_event), timeout=10)
    )

    row = _get_heartbeat(test_db_session, "medication_reminder")
    assert row is not None, "medication_reminder 未寫 heartbeat"
    assert row.last_success_at is not None
    assert row.expected_interval_seconds == 24 * 60 * 60


def test_auto_graduation_persists_heartbeat_even_when_not_due(
    test_db_session, monkeypatch
):
    """年度 job：未到畢業日（should_run=False）也要每巡檢 tick 寫 heartbeat，

    否則 /health/schedulers 在非畢業日（一年 364 天）永久 503。
    """
    from services import graduation_scheduler as sched

    stop_event = asyncio.Event()

    def _not_due(*a, **k):
        stop_event.set()  # 一個 tick 後即停
        return False

    monkeypatch.setattr(sched, "should_run_auto_graduation", _not_due)

    asyncio.run(
        asyncio.wait_for(sched.run_auto_graduation_scheduler(stop_event), timeout=10)
    )

    row = _get_heartbeat(test_db_session, "auto_graduation")
    assert (
        row is not None
    ), "auto_graduation 未到畢業日也應寫 heartbeat（liveness），否則 /health 永久 lagging"
    assert row.last_success_at is not None
    assert row.expected_interval_seconds == sched.CHECK_INTERVAL_SECONDS


# ── audit_log GC：法遵（個資法 §11 / 稅捐稽徵法 §30 七年）保留 job ───────────────
# scheduler_iteration("security_audit_log_gc") 原唯一漏帶 expected_interval_seconds →
# 不寫 scheduler_heartbeats row → /health/schedulers 與外部 watchdog 對這支法遵 GC 全盲
# （process restart 後看不到最近成功時間）。修：interval 由 scheduler 端的
# _AUDIT_LOG_GC_INTERVAL_SEC 往下傳進 run_audit_log_gc_once → scheduler_iteration。


def test_audit_log_gc_persists_heartbeat(test_db_session, monkeypatch):
    import types

    from services import security_gc_scheduler as sched

    # _run_audit_log_gc 在 settings.scheduler.audit_gc_enabled=False 時整段跳過 → 強制啟用
    monkeypatch.setattr(
        sched,
        "get_settings",
        lambda: types.SimpleNamespace(
            scheduler=types.SimpleNamespace(audit_gc_enabled=True)
        ),
    )

    sched._run_audit_log_gc()

    row = _get_heartbeat(test_db_session, "security_audit_log_gc")
    assert row is not None, "security_audit_log_gc 未寫 heartbeat（/health 全盲）"
    assert row.last_success_at is not None
    assert row.expected_interval_seconds == sched._AUDIT_LOG_GC_INTERVAL_SEC
