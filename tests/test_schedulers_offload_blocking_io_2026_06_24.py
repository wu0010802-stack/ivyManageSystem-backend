"""崩潰防護 P1：背景排程器的同步阻塞工作必須經 asyncio.to_thread 丟 threadpool。

問題：除 security_gc / recruitment_ivykids_sync 外，其餘排程器把同步 DB / 同步 LINE
推播 / 同步 Supabase 上傳 / 甚至同步外部 HTTP fetch（official_calendar）直接在
event loop 的 coroutine 內呼叫。單 uvicorn worker + WebSocket（接送/聯絡簿即時通知）
的部署模型下，上游慢或大批次掃描期間 event loop 被凍結 → 全站 API/WS/認證停擺。

修法：與 security_gc_scheduler 既有 canonical pattern 一致——
`with scheduler_iteration(...): X = await asyncio.to_thread(<sync_work>)`。

本測試：
- 行為測試（official_calendar，代表外部 HTTP 類）：證明同步工作真的被丟到 worker
  thread 跑，不在 event loop 主執行緒上阻塞。
- source-inspection（全部排程器 loop 函式）：每個 loop 都採用 asyncio.to_thread。
"""

import asyncio
import inspect
import threading

import pytest

# 各排程器的 async loop 函式（同步阻塞工作所在）
from services.academic_term_turnover_scheduler import (
    run_academic_term_turnover_scheduler,
)
from services.activity_waitlist_scheduler import run_activity_waitlist_scheduler
from services.announcement_publish_scheduler import run_announcement_publish_scheduler
from services.data_quality_scheduler import run_data_quality_scheduler
from services.finance_reconciliation_scheduler import (
    run_finance_reconciliation_scheduler,
)
from services.graduation_scheduler import run_auto_graduation_scheduler
from services.leave_quota_expiry_scheduler import run_leave_quota_expiry_scheduler
from services.line_token_health_scheduler import run_line_token_health_scheduler
from services.medication_reminder_scheduler import medication_reminder_loop
from services.notification.pending_uploads_scheduler import (
    run_pending_uploads_scheduler,
)
from services.notification.retry_scheduler import run_line_retry_scheduler
from services.official_calendar_scheduler import run_official_calendar_scheduler
from services.pii_retention_scheduler import run_pii_retention_scheduler
from services.recruitment_term_advance_scheduler import (
    run_recruitment_term_advance_scheduler,
)
from services.salary_snapshot_scheduler import run_salary_snapshot_scheduler

_LOOP_FUNCS = [
    run_official_calendar_scheduler,
    run_line_token_health_scheduler,
    run_pending_uploads_scheduler,
    run_line_retry_scheduler,
    run_announcement_publish_scheduler,
    run_data_quality_scheduler,
    run_finance_reconciliation_scheduler,
    run_salary_snapshot_scheduler,
    run_academic_term_turnover_scheduler,
    run_leave_quota_expiry_scheduler,
    run_recruitment_term_advance_scheduler,
    run_pii_retention_scheduler,
    run_auto_graduation_scheduler,
    run_activity_waitlist_scheduler,
    medication_reminder_loop,
]


@pytest.mark.parametrize("fn", _LOOP_FUNCS, ids=[f.__name__ for f in _LOOP_FUNCS])
def test_scheduler_loop_offloads_blocking_work_to_thread(fn):
    src = inspect.getsource(fn)
    assert (
        "asyncio.to_thread" in src
    ), f"{fn.__name__} 未用 asyncio.to_thread 丟阻塞工作 → 同步 IO 會凍結 event loop"


def test_official_calendar_sync_runs_off_event_loop_thread(monkeypatch):
    """official_calendar 的同步外部 HTTP fetch 必須在 worker thread 跑，不阻塞 loop。"""
    import services.official_calendar_scheduler as sched

    main_ident = threading.get_ident()
    recorded = {}
    called = threading.Event()

    def fake_sync(today=None):
        recorded["ident"] = threading.get_ident()
        called.set()
        return {}

    monkeypatch.setattr(sched, "sync_official_calendar_once", fake_sync)

    async def driver():
        stop = asyncio.Event()
        task = asyncio.create_task(sched.run_official_calendar_scheduler(stop))
        # 等 fake 被呼叫（記錄它在哪個 thread）
        for _ in range(200):
            if called.is_set():
                break
            await asyncio.sleep(0.01)
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:
            task.cancel()
        return recorded.get("ident")

    ident = asyncio.run(driver())
    assert ident is not None, "sync_official_calendar_once 未被呼叫"
    assert ident != main_ident, (
        "sync_official_calendar_once 在 event loop 主執行緒上跑（未經 to_thread）"
        "→ 外部 HTTP 慢時凍結全站"
    )
