"""Regression：in-memory SlidingWindowLimiter 跨測試累積污染防護。

根因（2026-06-23 全套件污染排查）：utils/rate_limit.SlidingWindowLimiter 的
`_timestamps` 是 module-global 限流器單例（如 api.leaves._batch_approve_limiter
= max_calls=10/60s）的實例狀態，import 時建立一次後在整個 pytest 程序內跨測試
累積，key=IP "testclient"。每個呼叫 /api/leaves/batch-approve 的測試都加一筆
時戳，跑到後段測試（test_leaves_overtimes_bug_batch_2026_05_11 在收集第 473
位）時 60s 視窗內累積已達上限 → 端點回 429（{'detail': '批次審核操作過於頻繁'}，
無 succeeded/failed 陣列）→ 後段測試斷言爆。app_client 只清 api.auth 的 legacy
in-memory dict + 換 DB engine，皆不碰此限流器。

修法：utils.rate_limit.reset_in_memory_limiters() 清空所有 in-memory 限流器，
由 conftest autouse fixture 於每個測試進場前呼叫，根除累積污染。
"""

import pytest
from fastapi import HTTPException

from utils.rate_limit import SlidingWindowLimiter, reset_in_memory_limiters


def test_reset_clears_accumulated_state():
    """累積到上限的 SlidingWindowLimiter，reset 後應重新從零開始。"""
    limiter = SlidingWindowLimiter(max_calls=2, window_seconds=60, name="iso_test")
    limiter.check("ip1")
    limiter.check("ip1")
    # 第 3 次超出上限 → 429
    with pytest.raises(HTTPException) as exc:
        limiter.check("ip1")
    assert exc.value.status_code == 429

    # reset 後，同一 key 應視同乾淨狀態，不再 raise
    reset_in_memory_limiters()
    limiter.check("ip1")  # 不應 raise


def test_reset_covers_all_registered_limiters():
    """新建 SlidingWindowLimiter 會自動註冊；reset 應一次涵蓋全部
    （含 api.leaves / api.overtimes 等 module-global 單例）。"""
    a = SlidingWindowLimiter(max_calls=1, window_seconds=60, name="iso_a")
    b = SlidingWindowLimiter(max_calls=1, window_seconds=60, name="iso_b")
    a.check("k")
    b.check("k")
    with pytest.raises(HTTPException):
        a.check("k")
    with pytest.raises(HTTPException):
        b.check("k")

    reset_in_memory_limiters()
    # 兩者都被清乾淨
    a.check("k")
    b.check("k")
