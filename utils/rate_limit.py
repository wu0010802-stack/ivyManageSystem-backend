"""
In-process sliding-window rate limiter (per IP).
適用單程序部署；若改用多程序/多機部署，應換成 Redis 版本。

⚠️ 生產環境部署注意事項：
──────────────────────────────────────────────────
此限流器使用 Python dict 儲存於記憶體中，僅在單一 worker process 內有效。

以下場景限流會失效：
  1. 多 worker 部署（如 gunicorn -w 4）→ 每個 worker 有獨立計數器
  2. 多機 / 多實例部署（如 K8s replicas > 1）→ 各實例無法共享狀態
  3. 程序重啟後計數器歸零

生產環境建議替代方案：
  - 使用 Redis-backed 限流（如 slowapi + redis / 自行實作）
  - 在反向代理層（Nginx / Cloudflare）設定 rate limiting
  - 兩層搭配使用：Nginx 做粗粒度限流 + 應用層做細粒度限流

目前此限流器適用於：
  - 單機開發環境
  - 單 worker 的小型部署
  - 搭配反向代理限流的第二層防護
──────────────────────────────────────────────────
"""

import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


class BaseLimiter(ABC):
    """限流器抽象介面。

    所有實作（in-memory、Redis、分散式…）均提供 `check(key)` 與 `as_dependency()`。
    使用端應以 BaseLimiter 型別宣告依賴，日後切換實作時無需修改呼叫點。
    """

    @abstractmethod
    def check(self, key: str) -> None:
        """檢查 key 是否超出限制；超出則拋 HTTPException(429)。"""

    def as_dependency(self):
        """回傳可用於 FastAPI Depends() 的函式，自動從 Request 取得來源 IP。"""
        limiter = self

        def _check(request: Request) -> None:
            key = request.client.host if request.client else "unknown"
            limiter.check(key)

        return _check


class SlidingWindowLimiter(BaseLimiter):
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
        now = time.time()
        ts = self._timestamps[key]
        self._timestamps[key] = [t for t in ts if now - t < self.window]
        if len(self._timestamps[key]) >= self.max_calls:
            logger.warning("Rate limit exceeded [%s] key=%s", self.name, key)
            raise HTTPException(status_code=429, detail=self.error_detail)
        self._timestamps[key].append(now)


class PostgresLimiter(BaseLimiter):
    """PostgreSQL 固定視窗限流器（LOW-1）

    用 `rate_limit_buckets` 表計數，UPSERT 原子操作；視窗以 `window_seconds` 切齊。

    與 SlidingWindowLimiter 的差異：
    - 固定視窗（floor(now / window) * window）：邊界附近兩個視窗合計可達 2x 限額。
      這是工程取捨：滑動視窗要存每筆 timestamp，PG 寫入成本高出許多。
    - 對暴力破解防護仍綽綽有餘；對「精準 RPS」場景才不夠用。

    DB 失敗時的策略：fail-open（log 警告，但不擋住正常請求）。
    避免 DB 短暫失聯時把所有 API 都 503。
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

    def check(self, key: str) -> None:
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import text

        from models.base import get_engine

        now = datetime.now(timezone.utc)
        bucket_start_epoch = (int(now.timestamp()) // self.window) * self.window
        window_start = datetime.fromtimestamp(bucket_start_epoch, tz=timezone.utc)
        bucket_key = f"{self.name}:{key}"

        try:
            engine = get_engine()
            with engine.begin() as conn:
                row = conn.execute(
                    text("""
                        INSERT INTO rate_limit_buckets (bucket_key, window_start, count)
                        VALUES (:bucket_key, :window_start, 1)
                        ON CONFLICT (bucket_key, window_start)
                        DO UPDATE SET count = rate_limit_buckets.count + 1
                        RETURNING count
                        """),
                    {"bucket_key": bucket_key, "window_start": window_start},
                ).fetchone()
                count = row[0] if row else 1
        except Exception as e:
            logger.warning(
                "PostgresLimiter [%s] DB 操作失敗，fail-open: %s",
                self.name,
                e,
            )
            return

        if count > self.max_calls:
            logger.warning(
                "Rate limit exceeded [%s] key=%s count=%s",
                self.name,
                key,
                count,
            )
            raise HTTPException(status_code=429, detail=self.error_detail)


def create_limiter(
    max_calls: int,
    window_seconds: int,
    name: str = "",
    error_detail: str = "請求過於頻繁，請稍後再試",
) -> BaseLimiter:
    """工廠函數：根據環境變數 RATE_LIMIT_BACKEND 決定限流器實作。

    - `postgres`：PG-backed（多 worker 安全；推薦 production）
    - `memory` 或未設定：in-process dict（單 worker 開發/小型部署）
    """
    import os

    backend = os.environ.get("RATE_LIMIT_BACKEND", "memory").lower()
    if backend == "postgres":
        return PostgresLimiter(
            max_calls=max_calls,
            window_seconds=window_seconds,
            name=name,
            error_detail=error_detail,
        )
    return SlidingWindowLimiter(
        max_calls=max_calls,
        window_seconds=window_seconds,
        name=name,
        error_detail=error_detail,
    )


def cleanup_rate_limit_buckets(retention_minutes: int = 60) -> int:
    """GC：刪除超過保留時間的舊視窗紀錄。回傳刪除筆數。

    通常每 5 分鐘呼叫一次（由 scheduler 排程）。
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import text

    from models.base import get_engine

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=retention_minutes)
    try:
        engine = get_engine()
        with engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM rate_limit_buckets WHERE window_start < :cutoff"),
                {"cutoff": cutoff},
            )
            return result.rowcount or 0
    except Exception as e:
        logger.warning("cleanup_rate_limit_buckets 失敗: %s", e)
        return 0
