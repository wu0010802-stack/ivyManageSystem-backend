"""排程器觀測性：metrics 收集 + 失敗 throttle 包裝器。

問題：所有 scheduler iteration 內既有 ``except Exception: logger.exception(...)``
+ Sentry ``LoggingIntegration(event_level=ERROR)`` → **每一次** 失敗都會自動產生
一個 Sentry event。transient 失敗（資料庫短暫不通、外部 API 抖動）會把 issue
看板淹掉。

設計：context manager + in-memory metrics singleton。

- 成功：reset ``consecutive_failures``、更新 ``last_success_at`` / ``last_rows_processed``
- 失敗：``consecutive_failures += 1``；達到 ``ALERT_THRESHOLD`` 才顯式 ``capture_exception``
- 例外 swallow：scheduler loop 自帶 retry，wrapper 不應讓 loop 中斷

使用方式：

.. code-block:: python

    from utils.scheduler_observability import scheduler_iteration, record_rows

    with scheduler_iteration("medication_reminder"):
        result = run_medication_reminder(effective_date=today)
        record_rows("medication_reminder", result.get("order_count", 0))

多 worker 限制（v1 設計，已知）：
metrics 是 per-process in-memory。``uvicorn --workers N`` 時，advisory_lock 保證
同 scheduler 只在拿鎖那個 worker 累計，但 ``/api/internal/metrics`` endpoint
回的是被路由到的 worker 觀點。endpoint 回傳含 ``worker_pid`` / ``hostname``
讓上游監控 scrape 多次再聚合。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator

from utils.sentry_init import capture_exception

logger = logging.getLogger(__name__)

# 連續失敗達此次數才上報 Sentry。1~2 次先 throttle 視為 transient。
ALERT_THRESHOLD = 3


def _should_line_alert(failure_count: int) -> bool:
    """LINE 告警節流：只在 ALERT_THRESHOLD 的冪次（3/9/27/81…）告警。

    跨過 threshold 那一次先告警，其後指數退避避免每次迭代轟炸，
    但持續惡化（失敗未止）仍會週期性再提醒。
    """
    if failure_count < ALERT_THRESHOLD:
        return False
    n = ALERT_THRESHOLD
    while n < failure_count:
        n *= ALERT_THRESHOLD
    return n == failure_count


@dataclass
class SchedulerStats:
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    consecutive_failures: int = 0
    last_rows_processed: int = 0
    total_rows_processed: int = 0
    total_runs: int = 0
    total_failures: int = 0
    last_error_message: str | None = None


class _MetricsStore:
    def __init__(self) -> None:
        self._stats: dict[str, SchedulerStats] = {}
        self._lock = threading.Lock()

    def get_or_create(self, name: str) -> SchedulerStats:
        with self._lock:
            stats = self._stats.get(name)
            if stats is None:
                stats = SchedulerStats()
                self._stats[name] = stats
            return stats

    def snapshot(self) -> dict[str, SchedulerStats]:
        with self._lock:
            return {
                name: SchedulerStats(
                    last_success_at=s.last_success_at,
                    last_failure_at=s.last_failure_at,
                    consecutive_failures=s.consecutive_failures,
                    last_rows_processed=s.last_rows_processed,
                    total_rows_processed=s.total_rows_processed,
                    total_runs=s.total_runs,
                    total_failures=s.total_failures,
                    last_error_message=s.last_error_message,
                )
                for name, s in self._stats.items()
            }

    def reset(self) -> None:
        with self._lock:
            self._stats.clear()


_METRICS = _MetricsStore()


def reset_for_tests() -> None:
    """測試用：清空全部 metrics。prod runtime 不該呼叫。"""
    _METRICS.reset()


def get_metrics_snapshot() -> dict[str, SchedulerStats]:
    """取得當下 metrics 快照（複製 dataclass 防外部 mutate）。"""
    return _METRICS.snapshot()


def get_worker_identity() -> dict[str, str | int]:
    """回傳 worker 識別欄位給 /api/internal/metrics endpoint 用。"""
    return {"worker_pid": os.getpid(), "hostname": socket.gethostname()}


def record_rows(scheduler_name: str, count: int) -> None:
    """記錄該次 iteration 處理筆數（會更新 last_rows_processed 與 total_rows_processed）。

    通常在 ``with scheduler_iteration(...)`` 區塊內呼叫，業務邏輯算完筆數再記。
    例外狀況不會走到這行，所以失敗時 last_rows_processed 維持舊值是預期行為。
    """
    stats = _METRICS.get_or_create(scheduler_name)
    with _METRICS._lock:
        stats.last_rows_processed = count
        stats.total_rows_processed += count


@contextmanager
def scheduler_iteration(
    scheduler_name: str,
    expected_interval_seconds: int | None = None,
) -> Iterator[None]:
    """執行一次 scheduler iteration，自動記 metrics + throttle Sentry 上報。

    成功：reset consecutive_failures、更新 last_success_at。
    失敗：consecutive_failures++；達 ALERT_THRESHOLD 才 capture_exception；
         例外被 swallow（讓 scheduler loop 繼續），不重 raise。

    Sentry 重複捕避免：失敗時用 ``logger.warning``（不會被 LoggingIntegration
    event_level=ERROR 自動抓），達閾值才透過 ``utils.sentry_init.capture_exception``
    顯式上報；保證每連串失敗最多上報 1 次（除非真的繼續累計新 3 次）。

    LINE 告警：連續失敗達 ALERT_THRESHOLD 的冪次（3/9/27…）時，透過
    ``services.ops_alert.notify_scheduler_failure`` 推 LINE group（group_id
    未設則 no-op）；指數退避避免每次迭代轟炸。告警自身例外 swallow。

    expected_interval_seconds：選填。若 caller 傳入則同時 UPSERT
    scheduler_heartbeats DB row（解決 in-memory metrics 在 process restart 後
    丟失最近成功時間的問題）。未傳則僅更新 in-memory 不寫 DB（既有 caller 行為
    不變）。DB 寫失敗 swallow（log warning），不影響 scheduler loop。
    """
    stats = _METRICS.get_or_create(scheduler_name)
    with _METRICS._lock:
        stats.total_runs += 1
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — scheduler iteration 故意 catch-all
        now = datetime.now(timezone.utc)
        with _METRICS._lock:
            stats.consecutive_failures += 1
            stats.total_failures += 1
            stats.last_failure_at = now
            stats.last_error_message = f"{type(exc).__name__}: {exc}"[:500]
            failure_count = stats.consecutive_failures
            last_error_message = stats.last_error_message
        if failure_count >= ALERT_THRESHOLD:
            logger.warning(
                "%s 連續 %d 次失敗，上報 Sentry: %s",
                scheduler_name,
                failure_count,
                exc,
                exc_info=True,
            )
            capture_exception(exc, level="error")
        if _should_line_alert(failure_count):
            try:
                # 延遲 import 避免 utils ↔ services 循環；以模組屬性呼叫
                # 讓測試可 monkeypatch ops_alert.notify_scheduler_failure。
                from services import ops_alert  # noqa: PLC0415

                ops_alert.notify_scheduler_failure(
                    scheduler_name=scheduler_name,
                    error=exc,
                    consecutive_failures=failure_count,
                )
            except Exception:  # noqa: BLE001 — 告警失敗不能炸 scheduler loop
                logger.warning(
                    "scheduler failure LINE alert 發送失敗 for %s",
                    scheduler_name,
                    exc_info=True,
                )
        if failure_count < ALERT_THRESHOLD:
            logger.warning(
                "%s 第 %d 次失敗（<%d 暫不上報 Sentry）: %s",
                scheduler_name,
                failure_count,
                ALERT_THRESHOLD,
                exc,
            )
        if expected_interval_seconds is not None:
            try:
                _persist_heartbeat(
                    scheduler_name=scheduler_name,
                    success=False,
                    error_message=last_error_message,
                    expected_interval_seconds=expected_interval_seconds,
                )
            except Exception:  # noqa: BLE001 — DB 寫失敗不能炸 scheduler loop
                logger.warning(
                    "scheduler_heartbeat DB persist failed (failure path) for %s",
                    scheduler_name,
                    exc_info=True,
                )
    else:
        now = datetime.now(timezone.utc)
        with _METRICS._lock:
            stats.consecutive_failures = 0
            stats.last_success_at = now
            stats.last_error_message = None
        if expected_interval_seconds is not None:
            try:
                _persist_heartbeat(
                    scheduler_name=scheduler_name,
                    success=True,
                    error_message=None,
                    expected_interval_seconds=expected_interval_seconds,
                )
            except Exception:  # noqa: BLE001 — DB 寫失敗不能炸 scheduler loop
                logger.warning(
                    "scheduler_heartbeat DB persist failed (success path) for %s",
                    scheduler_name,
                    exc_info=True,
                )


def _persist_heartbeat(
    scheduler_name: str,
    success: bool,
    error_message: str | None,
    expected_interval_seconds: int,
) -> None:
    """獨立 transaction 寫 scheduler_heartbeats row；用 session_scope 自動 commit。

    row 不存在時 upsert（避免新加 scheduler 漏掉 seed 也能 work）。
    呼叫端的 try/except 已包住，這裡保持 raise 讓 caller 決定 swallow 行為。
    """
    # 延遲 import 避免 utils 在 models 載入完成前被 import 造成循環。
    from models.base import session_scope  # noqa: PLC0415
    from models.scheduler_heartbeat import SchedulerHeartbeat  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        row = (
            session.query(SchedulerHeartbeat)
            .filter_by(scheduler_name=scheduler_name)
            .one_or_none()
        )
        if row is None:
            row = SchedulerHeartbeat(
                scheduler_name=scheduler_name,
                expected_interval_seconds=expected_interval_seconds,
            )
            session.add(row)
        else:
            # interval 可能因 config 變動，每次 tick 同步更新。
            row.expected_interval_seconds = expected_interval_seconds
        if success:
            row.last_success_at = now
            row.consecutive_failures = 0
            row.last_error_message = None
        else:
            row.last_failure_at = now
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
            row.last_error_message = (error_message or "")[:1000]
