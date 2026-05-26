"""PDF 背景生成 worker — bounded ThreadPoolExecutor 取代 FastAPI BackgroundTasks。

設計動機：
- 原本走 `BackgroundTasks.add_task(sync_def_fn)` 會丟進 starlette anyio threadpool
  （預設 40 slot），群體生成 200 張成長報告會打滿，連帶把其他 sync ORM 路由排隊。
- 改用 dedicated ThreadPoolExecutor(max_workers=N) 把 PDF job 跟 request-serving
  threadpool 隔離，避免互相干擾。submit() 是 fire-and-forget。

不做 in-process timeout 強制 kill thread（Python 限制：threading 無安全 cancel）；
PDF lib 內部若 hang 由 OS-level reverse-proxy 或外部 watchdog 處理。shutdown
逾時會 cancel queue 中尚未啟動的 job、放棄等待 in-flight，由 SIGKILL 收尾。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait as futures_wait
from typing import Callable, Optional, Set

from config import settings

logger = logging.getLogger(__name__)


_executor: Optional[ThreadPoolExecutor] = None
_job_callable: Optional[Callable[[int], None]] = None
_synchronous_for_tests: bool = False
_inflight: Set[Future] = set()
_inflight_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    """Lazy 初始化 executor singleton。"""
    global _executor
    if _executor is None:
        max_workers = max(1, settings.scheduler.pdf_worker_max_concurrency)
        _executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="pdf-worker",
        )
        logger.info("PDF worker executor initialized (max_workers=%d)", max_workers)
    return _executor


def configure_job_callable(fn: Callable[[int], None]) -> None:
    """由 api/portfolio/reports.py 在 import 時注入 `_generate_pdf_job`，
    避免 services/ → api/ 反向 import 循環。"""
    global _job_callable
    _job_callable = fn


def submit_pdf_job(report_id: int) -> Optional[Future]:
    """Fire-and-forget 提交 PDF 生成任務到 bounded executor。

    Test 模式（set_synchronous_for_tests(True)）下改在 caller thread 同步執行，
    避免 SQLite StaticPool 跨 thread 衝突；prod 一律走 executor。"""
    if _job_callable is None:
        raise RuntimeError(
            "pdf_worker.configure_job_callable() must be called at import time"
        )
    if _synchronous_for_tests:
        _safe_run(report_id)
        return None
    executor = _get_executor()
    future = executor.submit(_safe_run, report_id)
    with _inflight_lock:
        _inflight.add(future)
    future.add_done_callback(_discard_inflight)
    logger.info("PDF job queued: report_id=%d", report_id)
    return future


def _discard_inflight(fut: Future) -> None:
    with _inflight_lock:
        _inflight.discard(fut)


def set_synchronous_for_tests(flag: bool) -> None:
    """測試用：開啟後 submit_pdf_job() 改 inline 執行，不走 thread pool。"""
    global _synchronous_for_tests
    _synchronous_for_tests = flag


def _safe_run(report_id: int) -> None:
    """Worker 入口 wrapper：捕 exception 避免 thread silently 死掉。
    `_job_callable` 內部本身已有 try/except 標 failed，這層是最後保險。"""
    try:
        _job_callable(report_id)  # type: ignore[misc]
    except Exception:
        logger.exception("PDF worker uncaught exception: report_id=%d", report_id)


def shutdown(wait: bool = True, timeout: Optional[float] = None) -> None:
    """Lifespan shutdown 時呼叫。

    wait=True 時等待 in-flight job 完成；timeout 預設取
    settings.scheduler.pdf_worker_shutdown_timeout_seconds。逾時則 cancel 還在
    queue 中尚未啟動的 job + shutdown(wait=False) 立即釋放（已啟動的 thread
    無法強制中止，會繼續跑到完成或進程被 SIGKILL）。"""
    global _executor
    if _executor is None:
        return
    if timeout is None:
        timeout = float(settings.scheduler.pdf_worker_shutdown_timeout_seconds)
    logger.info(
        "PDF worker shutting down (wait=%s, timeout=%s, in-flight=%d)",
        wait,
        timeout,
        len(_inflight),
    )
    if wait:
        with _inflight_lock:
            pending = set(_inflight)
        if pending:
            done, not_done = futures_wait(pending, timeout=timeout)
            if not_done:
                logger.warning(
                    "PDF worker shutdown: %d job(s) still running after %.1fs, "
                    "abandoning (already-running threads keep running)",
                    len(not_done),
                    timeout,
                )
        _executor.shutdown(wait=False, cancel_futures=True)
    else:
        _executor.shutdown(wait=False, cancel_futures=True)
    _executor = None


def reset_for_tests() -> None:
    """測試用：強制丟掉現有 executor + job callable + sync flag，下次 submit 重建。"""
    global _executor, _job_callable, _synchronous_for_tests
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
    _executor = None
    _job_callable = None
    _synchronous_for_tests = False
    with _inflight_lock:
        _inflight.clear()
