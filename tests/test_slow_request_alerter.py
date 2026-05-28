"""Sliding window slow-request counter + cooldown 行為測試。

mock time.monotonic 與 services.ops_alert.notify_slow_request_burst，
以 deterministic 方式驗證窗口淘汰 + threshold 觸發 + per-path cooldown。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from config import settings
from utils import slow_request_alerter


@pytest.fixture(autouse=True)
def _reset_alerter():
    slow_request_alerter.reset_for_tests()
    yield
    slow_request_alerter.reset_for_tests()


@pytest.fixture
def cfg():
    """指向 settings.ops_alert；測試用預設值 (window=60s, threshold=10, cooldown=300s)。"""
    return settings.ops_alert


def _record(path: str, now: float, status: int = 200, elapsed_ms: float = 3000):
    with patch("utils.slow_request_alerter.time.monotonic", return_value=now):
        slow_request_alerter.record_slow(path, elapsed_ms, status)


def test_under_threshold_does_not_alert(cfg):
    """同 path 5 次（< threshold 10）→ 不觸發 alert。"""
    with patch("services.ops_alert.notify_slow_request_burst") as mock_notify:
        for i in range(5):
            _record("/api/foo", now=100.0 + i)
        assert mock_notify.call_count == 0


def test_at_threshold_triggers_alert(cfg):
    """同 path 達 threshold 10 次 → 觸發 alert 1 次。"""
    with patch("services.ops_alert.notify_slow_request_burst") as mock_notify:
        for i in range(cfg.slow_request_alert_threshold):
            _record("/api/foo", now=100.0 + i)
        assert mock_notify.call_count == 1
        call = mock_notify.call_args
        assert call.kwargs["path"] == "/api/foo"
        assert call.kwargs["count"] == cfg.slow_request_alert_threshold
        assert call.kwargs["window_seconds"] == cfg.slow_request_alert_window_seconds


def test_outside_window_records_are_evicted(cfg):
    """窗口外 timestamp 被 popleft 淘汰；達 threshold 前的舊紀錄不算入。"""
    with patch("services.ops_alert.notify_slow_request_burst") as mock_notify:
        # 先記 5 次在 t=100
        for i in range(5):
            _record("/api/bar", now=100.0 + i * 0.1)
        # 跳出窗口 (預設 60s) 後再記 5 次 → count 應為 5，未達 threshold
        for i in range(5):
            _record("/api/bar", now=200.0 + i * 0.1)
        assert mock_notify.call_count == 0


def test_cooldown_prevents_repeated_alert(cfg):
    """同 path 觸發 alert 後 cooldown 內再次達 threshold 不重發。"""
    with patch("services.ops_alert.notify_slow_request_burst") as mock_notify:
        # 第一波 10 次 → alert
        for i in range(cfg.slow_request_alert_threshold):
            _record("/api/baz", now=100.0 + i)
        # 隨即再 10 次（仍在 cooldown 內）→ 不該再 alert
        for i in range(cfg.slow_request_alert_threshold):
            _record("/api/baz", now=110.0 + i)
        assert mock_notify.call_count == 1


def test_alert_again_after_cooldown(cfg):
    """cooldown 過後同 path 達 threshold → 第二次 alert。"""
    with patch("services.ops_alert.notify_slow_request_burst") as mock_notify:
        for i in range(cfg.slow_request_alert_threshold):
            _record("/api/qux", now=100.0 + i)
        # 跳 cooldown + 之後再 10 次
        base = 100.0 + cfg.slow_request_alert_cooldown_seconds + 10
        for i in range(cfg.slow_request_alert_threshold):
            _record("/api/qux", now=base + i)
        assert mock_notify.call_count == 2


def test_different_paths_independent(cfg):
    """不同 path 各自獨立計數與 cooldown。"""
    with patch("services.ops_alert.notify_slow_request_burst") as mock_notify:
        for i in range(cfg.slow_request_alert_threshold):
            _record("/api/A", now=100.0 + i)
        for i in range(cfg.slow_request_alert_threshold):
            _record("/api/B", now=100.0 + i)
        assert mock_notify.call_count == 2
        paths = {c.kwargs["path"] for c in mock_notify.call_args_list}
        assert paths == {"/api/A", "/api/B"}
