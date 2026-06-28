"""KillSwitchMiddleware — env-driven maintenance / read-only 503 短路。

env-only：避開事故時 DB 可能掛；zeabur dashboard 直接 flip env 即生效。
搭配 config/ops.py OpsSettings。

註冊位置（main.py）：必須在 AuditMiddleware 之後 add（成為 Audit 的外層
wrapper），這樣 maintenance / read_only 503 不會寫 audit log，避免事故期間
噴大量「BLOCKED_*」紀錄。

Bypass paths（hardcoded，不走 env，須為「完整掛載前綴」path）：
- /health/live, /health/ready, /health/schedulers — UptimeRobot 仍能監控
- /api/internal/uptime-webhook                   — UptimeRobot 告警仍能進來
- /api/auth/login, /api/auth/refresh             — admin 緊急進入

⚠ middleware 比對的是 request.url.path（含 router 掛載前綴），auth router
prefix=/api/auth，故此處必須用完整 /api/auth/login（先前誤寫 /auth/login →
永不命中 → 維護模式下 admin 無法登入自救）。

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
            "/api/auth/login",
            "/api/auth/refresh",
        }
    )

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self.BYPASS_PATHS:
            return await call_next(request)

        # cold-start migration 韌性（對標稽核 P1 / boot-loop 止血）：app_lifespan 在
        # alembic migration 失敗時設 app.state.migration_ok=False 並進維護模式（不 raise，
        # 避免 boot-loop）。此處自動觸發 503——不在「半套 / 壞 schema」上服務業務流量。
        # 與 env 開關 maintenance_mode 並存（env 仍可手動觸發）；BYPASS_PATHS 已先放行，
        # 故 /health/* 探針與 /api/auth/login 自救不受影響。
        if getattr(request.app.state, "migration_ok", True) is False:
            return _kill_switch_response(
                code="MAINTENANCE_MODE",
                message="系統升級維護中，暫時無法提供服務，請稍後再試。",
            )

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
