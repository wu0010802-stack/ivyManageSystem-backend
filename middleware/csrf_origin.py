"""CSRF Origin/Referer middleware.

對 POST/PATCH/PUT/DELETE 強制檢查 Origin（fallback Referer）必在
config.network.cors_origins 白名單。GET/HEAD/OPTIONS skip（RFC 7231 safe methods）。

LINE webhook 與家長公開報名 path bypass（webhook 走 signature / public 走限流+audit）。

Spec: docs/superpowers/specs/2026-05-28-csrf-origin-middleware-design.md
"""

import logging
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

UNSAFE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})

# Bypass paths（寫死於 module 常數，新增需改 code + PR review）
CSRF_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/line/webhook",  # LINE webhook signature 驗證不靠 cookie
    "/api/activity/public/",  # 家長公開報名 by design 接受跨站 POST
)


def _extract_origin_from_referer(referer: str) -> str | None:
    """從 Referer URL 取 scheme://host[:port]，normalize default port。"""
    try:
        parts = urlsplit(referer)
        if not parts.scheme or not parts.netloc:
            return None
        netloc = parts.netloc
        if parts.scheme == "https" and netloc.endswith(":443"):
            netloc = netloc[: -len(":443")]
        elif parts.scheme == "http" and netloc.endswith(":80"):
            netloc = netloc[: -len(":80")]
        return f"{parts.scheme}://{netloc}"
    except Exception:
        return None


def _get_allowed_origins() -> list[str]:
    """重用 main.py 的 CORS_ORIGINS 計算邏輯（含 dev fallback）。

    與 main.py:702-708 的 CORS_ORIGINS 變數同源（settings.network.cors_origins +
    dev fallback）。日後 main.py 改 fallback 名單需同步本 helper。
    """
    from config import settings

    origins = list(settings.network.cors_origins or [])
    if not origins and settings.core.env.lower() in ("development", "dev", "local"):
        origins = [
            "http://localhost:5173",
            "http://localhost:3000",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:3000",
        ]
    return origins


class CSRFOriginCheckMiddleware(BaseHTTPMiddleware):
    """CSRF defense — Origin/Referer header check for unsafe methods.

    GET/HEAD/OPTIONS 不檢查（RFC 7231 safe methods）。
    Bypass path（webhook / public）跳過檢查。
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Safe methods 直接放行
        if request.method not in UNSAFE_METHODS:
            return await call_next(request)

        # Bypass path 直接放行
        path = request.url.path
        if any(path.startswith(p) for p in CSRF_EXEMPT_PREFIXES):
            return await call_next(request)

        # 取白名單（rebuild per request 避免 hot-reload 配置）
        allowed_origins = _get_allowed_origins()
        if not allowed_origins:
            logger.error(
                "CSRF middleware: cors_origins 空集合，拒絕所有 unsafe request"
            )
            return JSONResponse(
                {"detail": "CSRF check failed: no allowed origins configured"},
                status_code=403,
            )

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")

        # 優先 Origin（瀏覽器 POST 一定有；非標準 client 才可能缺）
        if origin:
            if origin in allowed_origins:
                return await call_next(request)
            logger.warning(
                "CSRF reject: origin=%s not in allowlist path=%s method=%s",
                origin,
                path,
                request.method,
            )
            return JSONResponse(
                {"detail": "CSRF check failed: origin not allowed"},
                status_code=403,
            )

        # Fallback Referer（部分舊瀏覽器 / 特殊 client）
        if referer:
            referer_origin = _extract_origin_from_referer(referer)
            if referer_origin and referer_origin in allowed_origins:
                return await call_next(request)
            logger.warning(
                "CSRF reject: referer=%s (origin=%s) not in allowlist path=%s method=%s",
                referer,
                referer_origin,
                path,
                request.method,
            )
            return JSONResponse(
                {"detail": "CSRF check failed: referer not allowed"},
                status_code=403,
            )

        # 都缺：嚴格 reject
        logger.warning(
            "CSRF reject: missing both origin and referer path=%s method=%s",
            path,
            request.method,
        )
        return JSONResponse(
            {"detail": "CSRF check failed: missing origin/referer"},
            status_code=403,
        )
