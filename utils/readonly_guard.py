"""唯讀模擬守衛：impersonation_mode==readonly 的 token 不可寫入任何端點。"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# readonly 身份仍須能退出模擬 / 登出
_EXEMPT_PATHS = {"/api/auth/end-impersonate", "/api/auth/logout"}


class ReadonlyImpersonationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method.upper() not in _MUTATING:
            return await call_next(request)
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        token = request.cookies.get("access_token")
        if not token:
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                token = auth.split(" ", 1)[1]
        if token:
            from utils.auth import decode_token_for_audit

            payload = decode_token_for_audit(token) or {}
            if payload.get("impersonation_mode") == "readonly":
                logger.info(
                    "唯讀模擬擋下寫入：impersonated_by=%s path=%s method=%s",
                    payload.get("impersonated_by"),
                    request.url.path,
                    request.method,
                )
                return JSONResponse(
                    status_code=403,
                    content={"detail": "唯讀預覽模式不可寫入"},
                )
        return await call_next(request)
