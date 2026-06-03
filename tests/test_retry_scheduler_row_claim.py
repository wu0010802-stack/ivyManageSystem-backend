"""tick_line_retry 須 row-level claim 避免多 worker 雙發 LINE（稽核 2026-06-03 P3-3）。

tick_line_retry 以無鎖 query 撈 pending retry row 後逐筆重發；多 worker 部署時兩個
worker 會撈到同批 row → 同一通知雙發 LINE。改以 with_for_update(skip_locked=True)
row-level claim，各 worker 鎖住並處理不同 row。Postgres row lock 無法在 SQLite 行為
重現，故依 repo 慣例以 source-inspection 斷言。
"""

import inspect

from services.notification.retry_scheduler import tick_line_retry


def test_tick_line_retry_claims_rows_with_skip_locked():
    src = inspect.getsource(tick_line_retry)
    assert (
        ".with_for_update(" in src
    ), "tick_line_retry 缺 row-level claim（多 worker 雙發風險）"
    assert "skip_locked" in src, "應以 skip_locked 讓各 worker 處理不同 row"
