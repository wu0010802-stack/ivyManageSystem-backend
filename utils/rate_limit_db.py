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
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from utils.fail_open import capture_fail_open

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RA-MED-2：in-process backstop（DB 失敗時 auth scope 降級用，per-worker）
# ---------------------------------------------------------------------------
# auth scope 集合：login / change-password / reset / parent bind / device-setup。
# 這些 scope 的 caller 傳 fail_closed=True；DB 失敗時改數 in-process 滑動視窗，
# 不再 fail-open 回 0（避免攻擊者打掛 DB 即繞過全部限流）。
# 非 auth scope（例 public_register）維持 fail-open，行為不變。
_AUTH_SCOPES = frozenset(
    {
        "login_ip",
        "login_account",
        "pwd_change_ip",
        "pwd_change_user",
        "pwd_reset_ip",
        "parent_bind",
        "parent_bind_additional",
        "parent_device_setup",
    }
)

# per-worker 滑動視窗 backstop：bucket_key -> list[monotonic timestamp]。
# 僅在 DB 寫入失敗或 auth scope record 時填充；正常情況以 DB 為準（multi-worker 一致），
# backstop 只是 DB 失聯時的降級安全網（單 worker 視角，寬鬆但非歸零）。
_inproc: dict[str, list[float]] = {}
_inproc_lock = threading.Lock()


def _inproc_key(scope: str, key: str) -> str:
    return f"{scope}:{key}"


def record_attempt_inproc(scope: str, key: str, *, window_seconds: int) -> None:
    """在 in-process backstop 記一次 attempt（附帶順手修剪過期時間戳）。"""
    bk = _inproc_key(scope, key)
    now = time.monotonic()
    cutoff = now - window_seconds
    with _inproc_lock:
        ts = [t for t in _inproc.get(bk, []) if t >= cutoff]
        ts.append(now)
        _inproc[bk] = ts


def _count_recent_inproc(scope: str, key: str, *, within_seconds: int) -> int:
    """數 in-process backstop 在 within_seconds 內的 attempt 數（順手修剪過期）。"""
    bk = _inproc_key(scope, key)
    now = time.monotonic()
    cutoff = now - within_seconds
    with _inproc_lock:
        ts = [t for t in _inproc.get(bk, []) if t >= cutoff]
        if ts:
            _inproc[bk] = ts
        else:
            _inproc.pop(bk, None)
        return len(ts)


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

    PG / SQLite 3.24+ 同走 INSERT ... ON CONFLICT DO UPDATE 單一原子操作；
    rate_limit_buckets 上的 UNIQUE(bucket_key, window_start) 保證並發安全。
    DB 失敗時 fail-open（log 警告），避免 DB 短暫失聯把所有 login 都 503。

    資安掃描 2026-05-07 P2：原 SQLite path 用 SELECT-then-INSERT/UPDATE 兩步，
    並發測試（特別是高 worker 數 / 平行 fixture）會出現 race 漏算。改用
    ON CONFLICT 後 SQLite 與 PG 行為一致。
    """
    if engine is None:
        from models.base import get_engine

        engine = get_engine()

    # RA-MED-2：auth scope 同時寫 in-process backstop，確保 DB 失聯時
    # count_recent_attempts(fail_closed=True) 仍有資料可數（不歸零）。
    if scope in _AUTH_SCOPES:
        record_attempt_inproc(scope, key, window_seconds=window_seconds)

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
                # SQLite 3.24+：ON CONFLICT DO UPDATE 同 PG 語法（excluded 為待插入列別名）
                conn.execute(
                    text("""
                        INSERT INTO rate_limit_buckets (bucket_key, window_start, count)
                        VALUES (:bk, :ws, 1)
                        ON CONFLICT(bucket_key, window_start)
                        DO UPDATE SET count = rate_limit_buckets.count + 1
                        """),
                    {"bk": bk, "ws": window_start},
                )
    except Exception as e:
        capture_fail_open("rate_limit_db.record_attempt", e, scope=scope, key=key)


def _count_from_db(scope: str, key: str, *, within_seconds: int, engine: object) -> int:
    """從 rate_limit_buckets SUM 最近 within_seconds 內的 count。

    抽成獨立純查詢函式：便於 count_recent_attempts 在 DB 失敗時分流到
    in-process backstop（fail_closed），亦便於測試 mock。
    """
    bk = _bucket_key(scope, key)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT COALESCE(SUM(count), 0) FROM rate_limit_buckets "
                "WHERE bucket_key = :bk AND window_start >= :cutoff"
            ),
            {"bk": bk, "cutoff": cutoff},
        ).fetchone()
        return int(row[0] if row and row[0] is not None else 0)


def count_recent_attempts(
    scope: str,
    key: str,
    *,
    within_seconds: int,
    engine: Optional[object] = None,
    fail_closed: bool = False,
) -> int:
    """SUM rate_limit_buckets.count for `bucket_key=scope:key` AND
    `window_start >= now - within_seconds`。

    DB 失敗時：
    - fail_closed=False（預設，非 auth scope）：回 0（fail-open）—— 寧可放行
      也不要被 DB 短暫失聯擋下；後續 record_attempt 仍會寫入，下次 check 生效。
    - fail_closed=True（auth scope，RA-MED-2）：改用 in-process backstop 計數
      （per-worker 滑動視窗，降級但非歸零），避免攻擊者打掛 DB 即繞過限流。
    """
    if engine is None:
        from models.base import get_engine

        engine = get_engine()

    try:
        return _count_from_db(scope, key, within_seconds=within_seconds, engine=engine)
    except Exception as e:
        capture_fail_open(
            "rate_limit_db.count_recent_attempts", e, scope=scope, key=key
        )
        if fail_closed:
            return _count_recent_inproc(scope, key, within_seconds=within_seconds)
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
        capture_fail_open("rate_limit_db.clear_attempts", e, scope=scope, key=key)
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
        capture_fail_open("rate_limit_db.earliest_attempt_at", e, scope=scope, key=key)
        return None
