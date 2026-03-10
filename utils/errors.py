"""
Safe error handling utilities.

Prevents leaking internal implementation details (DB structure, SQL fragments,
file paths, package versions) to API clients in production.
"""

import logging
import os

from fastapi import HTTPException

logger = logging.getLogger(__name__)

_is_dev = os.environ.get("ENV", "development").lower() in ("development", "dev", "local")

_GENERIC_500_MESSAGE = "系統內部錯誤，請聯繫管理員"


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
