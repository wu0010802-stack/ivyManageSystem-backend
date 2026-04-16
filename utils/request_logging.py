"""
utils/request_logging.py — 請求效能指標 + 結構化日誌中間件

功能：
1. 為每個請求生成 request_id（用於日誌關聯）
2. 記錄請求處理時間（response time）
3. 慢請求自動警告（> SLOW_REQUEST_THRESHOLD_MS）
"""

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("request")

SLOW_REQUEST_THRESHOLD_MS = 2000  # 超過 2 秒視為慢請求


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """記錄每個請求的處理時間與 request_id。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id

        start = time.monotonic()
        response: Response = await call_next(request)
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{elapsed_ms}ms"

        method = request.method
        path = request.url.path
        status = response.status_code

        if elapsed_ms > SLOW_REQUEST_THRESHOLD_MS:
            logger.warning(
                "SLOW %s %s → %d (%.1fms) [rid=%s]",
                method,
                path,
                status,
                elapsed_ms,
                request_id,
            )
        else:
            logger.info(
                "%s %s → %d (%.1fms) [rid=%s]",
                method,
                path,
                status,
                elapsed_ms,
                request_id,
            )

        return response
