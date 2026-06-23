"""
Safe error handling utilities.

Prevents leaking internal implementation details (DB structure, SQL fragments,
file paths, package versions) to API clients in production.
"""

import logging

from fastapi import HTTPException

from config import settings

logger = logging.getLogger(__name__)

_is_dev = settings.core.env.lower() in ("development", "dev", "local")

_GENERIC_500_MESSAGE = "系統內部錯誤，請聯繫管理員"

# PG 鎖爭用 / 逾時類 SQLSTATE：報名尖峰下屬「預期競態」，應回友善 409『請稍候再試』
# 而非 500，且不該噴 Sentry。55P03=lock_not_available（lock_timeout 觸發）、
# 40P01=deadlock_detected。statement_timeout(57014) 不納入——那代表非鎖的長查詢異常，
# 仍應視為 500 / 上報。
_LOCK_CONTENTION_PGCODES = frozenset({"55P03", "40P01"})


def is_lock_contention_error(e: Exception) -> bool:
    """是否為 PostgreSQL 鎖爭用 / 鎖逾時類暫態錯誤（lock_timeout / deadlock）。

    用於報名等高併發寫入熱路徑：搶不到列鎖時快速失敗，回 409 讓家長稍候重試，
    避免併到通用 500 與 Sentry 噪音。判斷 driver 例外（psycopg2/psycopg）的 pgcode。
    """
    pgcode = getattr(getattr(e, "orig", None), "pgcode", None)
    return pgcode in _LOCK_CONTENTION_PGCODES


def raise_safe_500(e: Exception, *, context: str = "") -> None:
    """Log the real error and raise a safe 500 HTTPException.

    In development: detail includes the original error message for debugging.
    In production:  detail is a generic message; the real error is only logged.

    Usage::

        except Exception as e:
            raise_safe_500(e, context="建立公告")
    """
    log_msg = f"{context}: {e}" if context else str(e)
    logger.error(log_msg, exc_info=True)

    if _is_dev:
        raise HTTPException(status_code=500, detail=str(e))
    else:
        raise HTTPException(status_code=500, detail=_GENERIC_500_MESSAGE)


_GENERIC_BATCH_REASON = "處理失敗，請稍後重試或聯絡管理員"


def safe_batch_reason(
    e: Exception,
    *,
    context: str = "",
    fallback: str = _GENERIC_BATCH_REASON,
) -> str:
    """批次操作 per-item 失敗原因的安全字串。

    批次端點常把 `str(e)` / f"...{e}" 放進回傳的 `failed[].reason`，會把非預期
    例外（DB 錯誤、constraint 名、SQL 片段）洩漏給 client。本 helper：

    - HTTPException：回其 `detail`（屬刻意的業務驗證訊息，例如「假單已封存」，安全）。
    - 其他例外：記錄完整例外到 log（exc_info）後，回傳不含內部細節的通用訊息。

    Usage::

        except Exception as e:
            failed.append({"id": x, "reason": safe_batch_reason(e, context="批次核准")})
    """
    if isinstance(e, HTTPException):
        return str(e.detail)
    logger.error("批次項目處理失敗%s", f"：{context}" if context else "", exc_info=True)
    return fallback
