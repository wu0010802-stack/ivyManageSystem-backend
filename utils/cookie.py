"""
HTTP-only cookie helpers for JWT token management.

Provides set/clear functions for access_token and admin_token cookies.
Cookies are configured with:
  - httponly=True       → JavaScript cannot read them (XSS protection)
  - samesite="strict"   → 完全擋下跨站請求帶 cookie；可由 COOKIE_SAMESITE env 覆寫（LOW-5）
  - secure              → True in production (HTTPS only), False in dev (HTTP)
  - path="/api"         → only sent for API requests

LOW-5 設計選擇：
  - 預設 SameSite=Strict（最強 CSRF 防護）
  - 環境變數 `COOKIE_SAMESITE` 可調為 lax（若 LIFF 或外部站連回流程受影響）
  - 不接受 none（會強迫 secure，且 CSRF 暴露面太大）
"""

import logging
import os

logger = logging.getLogger(__name__)

_is_dev = os.environ.get("ENV", "development").lower() in (
    "development",
    "dev",
    "local",
)


def _resolve_samesite() -> str:
    raw = os.environ.get("COOKIE_SAMESITE", "strict").lower()
    if raw not in ("strict", "lax"):
        logger.warning("COOKIE_SAMESITE=%s 不被支援，回退 strict", raw)
        return "strict"
    return raw


# Cookie 共用參數
_COOKIE_SECURE = not _is_dev  # 正式環境限 HTTPS
_COOKIE_SAMESITE = _resolve_samesite()
_COOKIE_PATH = "/api"
_COOKIE_MAX_AGE = 86400  # 24 小時（與 JWT refresh grace period 對齊）


def set_access_token_cookie(response, token: str) -> None:
    """在 response 上設定 access_token httpOnly Cookie。"""
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite=_COOKIE_SAMESITE,
        secure=_COOKIE_SECURE,
        path=_COOKIE_PATH,
        max_age=_COOKIE_MAX_AGE,
    )


def clear_access_token_cookie(response) -> None:
    """清除 access_token Cookie。"""
    response.delete_cookie(
        key="access_token",
        httponly=True,
        samesite=_COOKIE_SAMESITE,
        secure=_COOKIE_SECURE,
        path=_COOKIE_PATH,
    )


def set_admin_token_cookie(response, token: str) -> None:
    """在 response 上設定 admin_token httpOnly Cookie（冒充時備份管理員 Token）。"""
    response.set_cookie(
        key="admin_token",
        value=token,
        httponly=True,
        samesite=_COOKIE_SAMESITE,
        secure=_COOKIE_SECURE,
        path=_COOKIE_PATH,
        max_age=_COOKIE_MAX_AGE,
    )


def clear_admin_token_cookie(response) -> None:
    """清除 admin_token Cookie。"""
    response.delete_cookie(
        key="admin_token",
        httponly=True,
        samesite=_COOKIE_SAMESITE,
        secure=_COOKIE_SECURE,
        path=_COOKIE_PATH,
    )
