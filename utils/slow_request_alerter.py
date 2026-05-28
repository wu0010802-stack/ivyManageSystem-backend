"""慢請求 sliding window counter + dedupe 觸發器。

行為：
- record_slow(path, elapsed_ms, status) 由 RequestLoggingMiddleware 在慢請求
  分支呼叫
- 每 path 維護一個 sliding window deque[timestamp]，O(1) append/popleft
- 超過 ops_alert.slow_request_alert_threshold (預設 10/分鐘) 即觸發
  services.ops_alert.notify_slow_request_burst
- 同 path 5 分鐘 cooldown，避免單一壞端點刷螢幕
- 純 in-memory（process-local）；gunicorn 多 worker 各自獨立計數，
  threshold 與 worker count 的乘積決定真實觸發量（容忍 over-report；
  prod 設 X workers × 10 = 30+ 次/分才觸 alert，可接受）
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Deque

from config import settings

logger = logging.getLogger(__name__)

_window: dict[str, Deque[float]] = defaultdict(deque)
_last_alert: dict[str, float] = {}
_lock = threading.Lock()


def record_slow(path: str, elapsed_ms: float, status: int) -> None:
    """記錄一次慢請求；若達 threshold 且過 cooldown，觸發 LINE 告警。"""
    cfg = settings.ops_alert
    if not cfg.enabled:
        return

    now = time.monotonic()
    window_start = now - cfg.slow_request_alert_window_seconds

    with _lock:
        q = _window[path]
        q.append(now)
        while q and q[0] < window_start:
            q.popleft()
        count = len(q)
        last = _last_alert.get(path)  # None = 從未告警過
        in_cooldown = (
            last is not None and (now - last) < cfg.slow_request_alert_cooldown_seconds
        )

        if count >= cfg.slow_request_alert_threshold and not in_cooldown:
            _last_alert[path] = now
            should_alert = True
        else:
            should_alert = False

    if should_alert:
        # lazy import：services.ops_alert 內部 lazy import line_service，
        # 避免 utils → services → models 載入鏈在 middleware 啟動期循環。
        from services.ops_alert import notify_slow_request_burst

        notify_slow_request_burst(
            path=path,
            count=count,
            window_seconds=cfg.slow_request_alert_window_seconds,
            sample_elapsed_ms=elapsed_ms,
            sample_status=status,
        )


def reset_for_tests() -> None:
    """測試 helper：清空窗口與 cooldown 狀態。"""
    with _lock:
        _window.clear()
        _last_alert.clear()
