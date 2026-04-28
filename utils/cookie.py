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
  - 環境變數 `COOKIE_SAMESITE` 可調為 lax / none
  - 設 none 時會強制 Secure；dev 模式（HTTP）拒絕 none 並 fallback lax，
    避免本機端 cookie 被瀏覽器拒收
  - none 用於前後端跨網域部署（如 ivymanageportal.zeabur.app +
    ivymanagesystem-api.zeabur.app）；CSRF 暴露面靠 token_version + JWT
    黑名單 + 路由權限守衛收斂
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
    if raw not in ("strict", "lax", "none"):
        logger.warning("COOKIE_SAMESITE=%s 不被支援，回退 strict", raw)
        return "strict"
    if raw == "none" and _is_dev:
        logger.warning(
            "COOKIE_SAMESITE=none 需 Secure（HTTPS），dev 環境不支援，回退 lax"
        )
        return "lax"
    return raw


# Cookie 共用參數
_COOKIE_SAMESITE = _resolve_samesite()
# samesite=none 強制 Secure（瀏覽器規範）；其他模式維持 dev/prod 自動判斷
_COOKIE_SECURE = True if _COOKIE_SAMESITE == "none" else (not _is_dev)
_COOKIE_PATH = "/api"
_COOKIE_MAX_AGE = 86400  # 24 小時（與 JWT refresh grace period 對齊）


def get_cookie_samesite() -> str:
    """供其他模組（如 parent_portal/auth.py 的 bind cookie）共用同一份決策。"""
    return _COOKIE_SAMESITE


def get_cookie_secure() -> bool:
    """供其他模組共用 Secure 決策。"""
    return _COOKIE_SECURE


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
