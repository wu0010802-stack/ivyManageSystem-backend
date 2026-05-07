"""DB-backed login attempt counter（取代 in-process dict 限流）。

威脅：原本 api/auth.py 的 `_ip_attempts / _account_failures` 與
api/parent_portal/auth.py 的 `_bind_failures` 都是 per-process global
dict。多 worker 部署下每個 worker 各維護一份，攻擊者把請求分散到不同
worker 即可繞過 IP / account / bind 鎖定（audit 2026-05-07 P0 #14）。

設計：複用既有 `rate_limit_buckets` 表 + `RateLimitBucket` model（PG-only；
SQLite 用於測試）。三個 helper 對應 in-process dict 的三個操作：

- record_attempt(scope, key, window_seconds)
    INSERT/UPSERT 一筆 row（fixed window aligned to floor(now/window)*window），
    與 PostgresLimiter 對齊。

- count_recent_attempts(scope, key, within_seconds)
    SUM count from rows whose window_start >= now - within_seconds。
    fixed-window 的「最近 N 個視窗總和」估算；邊界誤差可接受（防暴力破解
    場景的閾值寬鬆，正常人工操作不會撞線）。

- clear_attempts(scope, key)
    DELETE 該 key 全部 rows（登入成功後清除帳號失敗計數）。

Backend 切換：與既有 PostgresLimiter / SlidingWindowLimiter 一致，
透過環境變數 RATE_LIMIT_BACKEND 控制（postgres / memory）。但 login /
bind 流程的 limiter 必須是 multi-worker 安全的，所以本 helper 直接走
DB 不提供 memory fallback；測試用 SQLite。

Refs: 邏輯漏洞 audit 2026-05-07 P0 #14（user 拍板採 DB-backed 方案）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)


def _bucket_key(scope: str, key: str) -> str:
    """組合 bucket_key：scope 隔開不同 limiter 的 namespace。"""
    return f"{scope}:{key}"


def _floored_window_start(window_seconds: int) -> datetime:
    """fixed-window 對齊：floor(now / window) * window。"""
    now = datetime.now(timezone.utc)
    bucket_start_epoch = (int(now.timestamp()) // window_seconds) * window_seconds
    return datetime.fromtimestamp(bucket_start_epoch, tz=timezone.utc)


def record_attempt(
    scope: str, key: str, *, window_seconds: int, engine: Optional[object] = None
) -> None:
    """記錄一次 attempt（INSERT/UPSERT 累積 count）。

    PG: ON CONFLICT 累加；SQLite: 先 SELECT 後決定 UPDATE/INSERT。
    DB 失敗時 fail-open（log 警告），避免 DB 短暫失聯把所有 login 都 503。
    """
    if engine is None:
        from models.base import get_engine

        engine = get_engine()

    bk = _bucket_key(scope, key)
    window_start = _floored_window_start(window_seconds)

    try:
        is_pg = engine.dialect.name == "postgresql"
        with engine.begin() as conn:
            if is_pg:
                conn.execute(
                    text("""
                        INSERT INTO rate_limit_buckets (bucket_key, window_start, count)
                        VALUES (:bk, :ws, 1)
                        ON CONFLICT (bucket_key, window_start)
                        DO UPDATE SET count = rate_limit_buckets.count + 1
                        """),
                    {"bk": bk, "ws": window_start},
                )
            else:
                # SQLite fallback：SELECT 後 UPDATE，失敗則 INSERT
                row = conn.execute(
                    text(
                        "SELECT count FROM rate_limit_buckets "
                        "WHERE bucket_key = :bk AND window_start = :ws"
                    ),
                    {"bk": bk, "ws": window_start},
                ).fetchone()
                if row is not None:
                    conn.execute(
                        text(
                            "UPDATE rate_limit_buckets SET count = count + 1 "
                            "WHERE bucket_key = :bk AND window_start = :ws"
                        ),
                        {"bk": bk, "ws": window_start},
                    )
                else:
                    conn.execute(
                        text(
                            "INSERT INTO rate_limit_buckets "
                            "(bucket_key, window_start, count) VALUES (:bk, :ws, 1)"
                        ),
                        {"bk": bk, "ws": window_start},
                    )
    except Exception as e:
        logger.warning("record_attempt 失敗 [%s:%s]: %s", scope, key, e)


def count_recent_attempts(
    scope: str,
    key: str,
    *,
    within_seconds: int,
    engine: Optional[object] = None,
) -> int:
    """SUM rate_limit_buckets.count for `bucket_key=scope:key` AND
    `window_start >= now - within_seconds`。

    DB 失敗時回 0（fail-open）—— login 路徑寧可放行也不要被 DB 短暫失聯
    擋下；後續的 record_attempt 仍會嘗試寫入，下一次 check 會生效。
    """
    if engine is None:
        from models.base import get_engine

        engine = get_engine()

    bk = _bucket_key(scope, key)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)

    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT COALESCE(SUM(count), 0) FROM rate_limit_buckets "
                    "WHERE bucket_key = :bk AND window_start >= :cutoff"
                ),
                {"bk": bk, "cutoff": cutoff},
            ).fetchone()
            return int(row[0] if row and row[0] is not None else 0)
    except Exception as e:
        logger.warning("count_recent_attempts 失敗 [%s:%s]: %s", scope, key, e)
        return 0


def clear_attempts(scope: str, key: str, *, engine: Optional[object] = None) -> int:
    """DELETE 所有 rows for bucket_key = scope:key。回傳刪除筆數。

    用於登入/驗證成功時清除累積失敗計數。DB 失敗時回 0（fail-open）。
    """
    if engine is None:
        from models.base import get_engine

        engine = get_engine()

    bk = _bucket_key(scope, key)
    try:
        with engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM rate_limit_buckets WHERE bucket_key = :bk"),
                {"bk": bk},
            )
            return result.rowcount or 0
    except Exception as e:
        logger.warning("clear_attempts 失敗 [%s:%s]: %s", scope, key, e)
        return 0


def earliest_attempt_at(
    scope: str,
    key: str,
    *,
    within_seconds: int,
    engine: Optional[object] = None,
) -> Optional[datetime]:
    """回傳該 key 在 within_seconds 內最早一筆 row 的 window_start；無則 None。

    用於 account lockout 顯示「剩餘解鎖時間」。
    """
    if engine is None:
        from models.base import get_engine

        engine = get_engine()

    bk = _bucket_key(scope, key)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT MIN(window_start) FROM rate_limit_buckets "
                    "WHERE bucket_key = :bk AND window_start >= :cutoff"
                ),
                {"bk": bk, "cutoff": cutoff},
            ).fetchone()
            return row[0] if row and row[0] is not None else None
    except Exception as e:
        logger.warning("earliest_attempt_at 失敗 [%s:%s]: %s", scope, key, e)
        return None
