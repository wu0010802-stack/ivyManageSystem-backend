"""
utils/request_logging.py — 請求效能指標 + 結構化日誌中間件

功能：
1. 為每個請求生成 request_id（用於日誌關聯）
2. 灌進 contextvars：所有 logger 透過 RequestIdLogFilter 自動帶 request_id 欄位
3. 灌進 Sentry：每個 event 自動帶 request_id tag（FastApiIntegration 已為 per-request 建 scope）
4. 記錄請求處理時間（response time）+ 慢請求警告（> SLOW_REQUEST_THRESHOLD_MS）
"""

import contextvars
import logging
import time
import uuid

import sentry_sdk
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("request")

SLOW_REQUEST_THRESHOLD_MS = 2000  # 超過 2 秒視為慢請求

# Per-request 識別碼。預設 "-" 讓啟動期 log（middleware 還沒套到的）能正常 format。
# 跨 asyncio.Task 自動繼承（create_task copy context）；中介層用 reset(token) 清乾淨。
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


class RequestIdLogFilter(logging.Filter):
    """把 ContextVar 內的 request_id 注入 LogRecord，formatter 才能引用 %(request_id)s。

    所有 logger（不只 `request`）只要 handler 上掛了這個 filter 就會自動帶。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """記錄每個請求的處理時間與 request_id。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id

        token = request_id_var.set(request_id)
        # Sentry FastApiIntegration 已為 per-request 開 scope；set_tag 落該 scope。
        # SDK 未 init（DSN 缺）時 set_tag 自動 no-op。
        sentry_sdk.set_tag("request_id", request_id)

        start = time.monotonic()
        try:
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
        finally:
            request_id_var.reset(token)
