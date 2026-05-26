"""Tests for services/pdf_worker.py — bounded executor for PDF generation."""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import pdf_worker


@pytest.fixture(autouse=True)
def _isolated_worker():
    pdf_worker.reset_for_tests()
    yield
    pdf_worker.reset_for_tests()


def test_submit_runs_callable_with_report_id():
    received: list[int] = []
    done = threading.Event()

    def job(report_id: int) -> None:
        received.append(report_id)
        done.set()

    pdf_worker.configure_job_callable(job)
    pdf_worker.submit_pdf_job(42)
    assert done.wait(timeout=2.0), "job should run within 2s"
    assert received == [42]


def test_submit_without_configure_raises():
    with pytest.raises(RuntimeError):
        pdf_worker.submit_pdf_job(1)


def test_callable_exception_does_not_crash_worker():
    """Job 拋例外不應該把整個 worker thread 弄死。"""
    flaky_done = threading.Event()
    next_done = threading.Event()

    def job(report_id: int) -> None:
        if report_id == 1:
            flaky_done.set()
            raise RuntimeError("synthetic failure")
        next_done.set()

    pdf_worker.configure_job_callable(job)
    pdf_worker.submit_pdf_job(1)
    assert flaky_done.wait(timeout=2.0)
    # 再丟一個 job，executor 應該還能跑
    pdf_worker.submit_pdf_job(2)
    assert next_done.wait(timeout=2.0), "executor should still accept new jobs"


def test_max_concurrency_bound(monkeypatch):
    """確認 max_workers=2 時最多 2 個 job 同時跑。"""
    from config import settings

    monkeypatch.setattr(settings.scheduler, "pdf_worker_max_concurrency", 2)

    in_flight = 0
    max_observed = 0
    lock = threading.Lock()
    release = threading.Event()
    started = threading.Event()
    start_count = 0

    def job(report_id: int) -> None:
        nonlocal in_flight, max_observed, start_count
        with lock:
            in_flight += 1
            start_count += 1
            if in_flight > max_observed:
                max_observed = in_flight
            if start_count >= 2:
                started.set()
        release.wait(timeout=5.0)
        with lock:
            in_flight -= 1

    pdf_worker.configure_job_callable(job)
    for rid in range(6):
        pdf_worker.submit_pdf_job(rid)

    # 等到至少 2 個 job 已經進到 release.wait()
    assert started.wait(timeout=2.0), "expected 2 concurrent jobs"
    # 給一點時間確認不會有第 3 個 slot 跑起來
    time.sleep(0.2)
    assert max_observed == 2, f"expected ≤2 concurrent, observed {max_observed}"

    release.set()
    pdf_worker.shutdown(wait=True)
    assert in_flight == 0


def test_shutdown_waits_for_inflight_when_wait_true():
    completed: list[int] = []
    release = threading.Event()

    def job(report_id: int) -> None:
        release.wait(timeout=5.0)
        completed.append(report_id)

    pdf_worker.configure_job_callable(job)
    pdf_worker.submit_pdf_job(1)
    # release after small delay so shutdown(wait=True) actually waits
    threading.Timer(0.1, release.set).start()
    pdf_worker.shutdown(wait=True)
    assert completed == [1], "shutdown(wait=True) should block until job finishes"


def test_reset_for_tests_clears_state():
    pdf_worker.configure_job_callable(lambda rid: None)
    pdf_worker.submit_pdf_job(1)
    pdf_worker.reset_for_tests()
    with pytest.raises(RuntimeError):
        pdf_worker.submit_pdf_job(2)


def test_shutdown_timeout_abandons_long_jobs(caplog):
    """超過 timeout 的 in-flight job 不阻塞 shutdown，只在 log 留紀錄。"""
    hold = threading.Event()
    finished = threading.Event()

    def job(report_id: int) -> None:
        hold.wait(timeout=10.0)
        finished.set()

    pdf_worker.configure_job_callable(job)
    pdf_worker.submit_pdf_job(1)
    # 給 worker 一點時間進到 hold.wait
    time.sleep(0.05)

    import time as _t

    t0 = _t.monotonic()
    with caplog.at_level("WARNING"):
        pdf_worker.shutdown(wait=True, timeout=0.2)
    elapsed = _t.monotonic() - t0

    assert elapsed < 1.0, f"shutdown timeout should be ~0.2s, was {elapsed:.2f}s"
    assert any(
        "still running" in r.message for r in caplog.records
    ), "expected warning log about abandoned in-flight job"
    # 釋放讓 worker thread 結束（避免測試 leak）
    hold.set()
    finished.wait(timeout=2.0)


def test_synchronous_mode_runs_inline_no_executor():
    received: list[int] = []
    pdf_worker.configure_job_callable(lambda rid: received.append(rid))
    pdf_worker.set_synchronous_for_tests(True)

    fut = pdf_worker.submit_pdf_job(99)
    assert fut is None, "synchronous mode should return None (no future)"
    assert received == [99], "callable should run inline before submit returns"
