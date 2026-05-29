"""KillSwitchMiddleware — env-driven maintenance / read-only 503 短路。

env-only：避開事故時 DB 可能掛；zeabur dashboard 直接 flip env 即生效。
搭配 config/ops.py OpsSettings。

註冊位置（main.py）：必須在 AuditMiddleware 之後 add（成為 Audit 的外層
wrapper），這樣 maintenance / read_only 503 不會寫 audit log，避免事故期間
噴大量「BLOCKED_*」紀錄。

Bypass paths（hardcoded，不走 env）：
- /health/live, /health/ready, /health/schedulers — UptimeRobot 仍能監控
- /api/internal/uptime-webhook                   — UptimeRobot 告警仍能進來
- /auth/login, /auth/refresh                     — admin 緊急進入

Spec：docs/superpowers/specs/2026-05-28-observability-killswitch-friendly-error-design.md §4
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

from config import get_settings

# 503 envelope retry 提示秒數（Retry-After header + body retry_after 對齊）
RETRY_AFTER_SECONDS = 300

# 唯讀模式擋下的 mutation 方法；其餘（GET / HEAD / OPTIONS / TRACE）放行
_MUTATION_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# 唯讀模式預設訊息（read_only 不從 env 客製 message，與 maintenance_message 分離）
_READ_ONLY_DEFAULT_MESSAGE = "系統暫時唯讀，編輯功能暫不可用"


class KillSwitchMiddleware(BaseHTTPMiddleware):
    """env-driven 維護/唯讀 503 短路 middleware。

    每個 request 進 `dispatch()` 時：
    1. path 在 BYPASS_PATHS 直接 pass through（不論 flag）
    2. ops.maintenance_mode=True → 全 method 503 MAINTENANCE_MODE
    3. ops.read_only_mode=True 且 method 是 mutation → 503 READ_ONLY_MODE
    4. 其餘 pass through
    """

    BYPASS_PATHS: frozenset[str] = frozenset(
        {
            "/health/live",
            "/health/ready",
            "/health/schedulers",
            "/api/internal/uptime-webhook",
            "/auth/login",
            "/auth/refresh",
        }
    )

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self.BYPASS_PATHS:
            return await call_next(request)

        ops = get_settings().ops

        if ops.maintenance_mode:
            return _kill_switch_response(
                code="MAINTENANCE_MODE",
                message=ops.maintenance_message,
            )

        if ops.read_only_mode and request.method.upper() in _MUTATION_METHODS:
            return _kill_switch_response(
                code="READ_ONLY_MODE",
                message=_READ_ONLY_DEFAULT_MESSAGE,
            )

        return await call_next(request)


def _kill_switch_response(*, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "message": message,
                "code": code,
                "retry_after": RETRY_AFTER_SECONDS,
            }
        },
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )
