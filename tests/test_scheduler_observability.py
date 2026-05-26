"""utils/scheduler_observability.py 單元測試。

驗證：
- 成功 reset consecutive_failures、更新 last_success_at
- 連續失敗 < ALERT_THRESHOLD 不上報 Sentry
- 連續失敗 >= ALERT_THRESHOLD 才呼叫 capture_exception
- 成功後再失敗從 1 重新計數
- record_rows 同時更新 last_rows_processed 與 total_rows_processed
- exception 被 swallow（caller 不會看到）
- get_metrics_snapshot 回複本，外部 mutate 不影響原狀態
- worker_identity 含 pid/hostname
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from utils import scheduler_observability as so


@pytest.fixture(autouse=True)
def _reset_metrics():
    so.reset_for_tests()
    yield
    so.reset_for_tests()


def test_success_records_last_success_and_increments_runs():
    with so.scheduler_iteration("alpha"):
        pass
    snap = so.get_metrics_snapshot()
    assert "alpha" in snap
    assert snap["alpha"].last_success_at is not None
    assert snap["alpha"].consecutive_failures == 0
    assert snap["alpha"].total_runs == 1
    assert snap["alpha"].total_failures == 0


def test_failure_swallowed_and_counted():
    with so.scheduler_iteration("beta"):
        raise RuntimeError("boom")
    snap = so.get_metrics_snapshot()
    assert snap["beta"].consecutive_failures == 1
    assert snap["beta"].total_failures == 1
    assert snap["beta"].last_failure_at is not None
    assert "RuntimeError" in (snap["beta"].last_error_message or "")


def test_failures_below_threshold_skip_sentry_capture():
    with patch.object(so, "capture_exception") as mock_capture:
        for _ in range(so.ALERT_THRESHOLD - 1):
            with so.scheduler_iteration("gamma"):
                raise RuntimeError("transient")
        mock_capture.assert_not_called()


def test_failures_reaching_threshold_call_sentry_capture():
    with patch.object(so, "capture_exception") as mock_capture:
        for _ in range(so.ALERT_THRESHOLD):
            with so.scheduler_iteration("delta"):
                raise RuntimeError("persistent")
        assert mock_capture.call_count == 1
        args, kwargs = mock_capture.call_args
        assert isinstance(args[0], RuntimeError)
        assert kwargs.get("level") == "error"


def test_continued_failures_keep_capturing():
    """達閾值後再失敗仍會每次上報（不去 dedupe，由 Sentry 端去重）。"""
    with patch.object(so, "capture_exception") as mock_capture:
        for _ in range(so.ALERT_THRESHOLD + 2):
            with so.scheduler_iteration("epsilon"):
                raise RuntimeError("still failing")
        assert mock_capture.call_count == 3  # threshold + 2


def test_success_resets_consecutive_failures():
    with so.scheduler_iteration("zeta"):
        raise RuntimeError("first fail")
    with so.scheduler_iteration("zeta"):
        raise RuntimeError("second fail")
    snap = so.get_metrics_snapshot()
    assert snap["zeta"].consecutive_failures == 2

    with so.scheduler_iteration("zeta"):
        pass

    snap = so.get_metrics_snapshot()
    assert snap["zeta"].consecutive_failures == 0
    assert snap["zeta"].total_runs == 3
    assert snap["zeta"].total_failures == 2


def test_success_after_threshold_resets_then_resentry_on_next_burst():
    """成功後 reset；下一輪要重新累計到 3 才再次上報。"""
    with patch.object(so, "capture_exception") as mock_capture:
        for _ in range(so.ALERT_THRESHOLD):
            with so.scheduler_iteration("eta"):
                raise RuntimeError("first burst")
        assert mock_capture.call_count == 1

        with so.scheduler_iteration("eta"):
            pass

        # 下一輪：再 2 次失敗不應上報
        for _ in range(so.ALERT_THRESHOLD - 1):
            with so.scheduler_iteration("eta"):
                raise RuntimeError("second burst")
        assert mock_capture.call_count == 1


def test_record_rows_updates_last_and_total():
    so.record_rows("theta", 10)
    so.record_rows("theta", 5)
    snap = so.get_metrics_snapshot()
    assert snap["theta"].last_rows_processed == 5
    assert snap["theta"].total_rows_processed == 15


def test_snapshot_is_copy_not_reference():
    with so.scheduler_iteration("iota"):
        pass
    snap1 = so.get_metrics_snapshot()
    snap1["iota"].consecutive_failures = 999
    snap2 = so.get_metrics_snapshot()
    assert snap2["iota"].consecutive_failures == 0


def test_get_worker_identity_returns_pid_and_hostname():
    identity = so.get_worker_identity()
    assert "worker_pid" in identity
    assert "hostname" in identity
    assert isinstance(identity["worker_pid"], int)
    assert isinstance(identity["hostname"], str)


def test_multiple_schedulers_isolated():
    with so.scheduler_iteration("scheduler_a"):
        raise RuntimeError("a failed")
    with so.scheduler_iteration("scheduler_b"):
        pass

    snap = so.get_metrics_snapshot()
    assert snap["scheduler_a"].consecutive_failures == 1
    assert snap["scheduler_b"].consecutive_failures == 0
    assert snap["scheduler_a"].last_success_at is None
    assert snap["scheduler_b"].last_success_at is not None
