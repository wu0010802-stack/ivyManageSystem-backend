"""
In-process sliding-window rate limiter (per IP).
適用單程序部署；若改用多程序/多機部署，應換成 Redis 版本。
"""

import logging
import time
from collections import defaultdict

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


class SlidingWindowLimiter:
    """滑動視窗限流器（記憶體版）

    Args:
        max_calls:       視窗內允許的最大請求數
        window_seconds:  視窗時間（秒）
        name:            限流器名稱，用於 log
        error_detail:    429 回傳的錯誤訊息
    """

    def __init__(
        self,
        max_calls: int,
        window_seconds: int,
        name: str = "",
        error_detail: str = "請求過於頻繁，請稍後再試",
    ):
        self.max_calls = max_calls
        self.window = window_seconds
        self.name = name
        self.error_detail = error_detail
        self._timestamps: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> None:
        """檢查 key 是否超出限制，超出則拋 429。"""
        now = time.time()
        ts = self._timestamps[key]
        self._timestamps[key] = [t for t in ts if now - t < self.window]
        if len(self._timestamps[key]) >= self.max_calls:
            logger.warning("Rate limit exceeded [%s] key=%s", self.name, key)
            raise HTTPException(status_code=429, detail=self.error_detail)
        self._timestamps[key].append(now)

    def as_dependency(self):
        """回傳可用於 FastAPI Depends() 的函式，自動從 Request 取得來源 IP。"""
        limiter = self

        def _check(request: Request) -> None:
            key = request.client.host if request.client else "unknown"
            limiter.check(key)

        return _check
