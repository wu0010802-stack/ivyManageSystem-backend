"""utils/exception_handlers.py — 全域 exception handler 與 envelope 規範。

四個 handler 對應四類例外：

| Exception                       | Envelope?         | Sentry?                |
|---------------------------------|-------------------|------------------------|
| BusinessError                   | Yes (envelope)    | http_status >= 500     |
| StarletteHTTPException (含 FastAPI) | No (透傳原 shape) | status >= 500          |
| RequestValidationError (422)    | Yes (envelope)    | 永不送（純使用者錯）    |
| Exception (unhandled)           | Yes (envelope)    | 一律送                  |

Envelope shape::

    {"detail": {"code": "...", "message": "...", "request_id": "..."}}

`request_id` 來自 utils/request_logging 注入的 request.state.request_id；缺值時
handler 自行生 fallback id 避免回應遺漏（exception 在 RequestLogging 之前發生的極罕場景）。

Sentry tag 規範（push_scope 不污染全域）：
- request_id：對齊回應 envelope 與 X-Request-ID header，事故追蹤用
- route：route template（e.g. /api/students/{student_id}），避免每筆 id 都炸新 issue
- user_id：silent JWT decode 取，與 sentry_init 既有 _hash_user_id 一致
- error_code：BusinessError 用實際 code；HTTPException fallback 用 HTTP_<status>
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from utils.exceptions import BusinessError

logger = logging.getLogger(__name__)

_GENERIC_500_MESSAGE = "系統內部錯誤，請聯繫管理員"
_GENERIC_422_MESSAGE = "輸入資料驗證失敗"


def _get_request_id(request: Request) -> str:
    """從 request.state 取 request_id；fallback 自行生（極罕：exception 在 RequestLogging 之前）。"""
    return getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]


def _get_route_template(request: Request) -> str:
    """取 route template (e.g. /api/students/{student_id})；routing 未完成或無 match 時降級 sanitized path。"""
    route = request.scope.get("route")
    template = getattr(route, "path", None) if route is not None else None
    if template:
        return template
    try:
        from utils.sentry_init import _sanitize_url

        return _sanitize_url(request.url.path)
    except Exception:
        return request.url.path


def _silent_user_id(request: Request) -> Any:
    """重用 audit._extract_user_from_header 的 silent JWT decode；無 token / 解析失敗皆回 None。"""
    try:
        from utils.audit import _extract_user_from_header

        user_id, _ = _extract_user_from_header(request)
        return user_id
    except Exception:
        return None


def _capture_to_sentry(
    exc: Exception,
    *,
    request: Request,
    request_id: str,
    error_code: str,
) -> None:
    """以 push_scope 上送 exception；sentry 未啟用 / SDK 未裝皆 silent no-op。"""
    try:
        import sentry_sdk
    except ImportError:
        return
    try:
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("request_id", request_id)
            scope.set_tag("route", _get_route_template(request))
            scope.set_tag("error_code", error_code)
            user_id = _silent_user_id(request)
            if user_id is not None:
                # _scrub_event 會把 user.id 過 _hash_user_id；這裡丟原 id 即可
                scope.set_user({"id": user_id})
            sentry_sdk.capture_exception(exc)
    except Exception as e:  # noqa: BLE001 — Sentry 失敗不可影響回應
        logger.warning(f"Sentry capture failed in exception handler: {e}")


def _envelope(
    *,
    code: str,
    message: str,
    request_id: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """組 envelope；extra 各 key merge 進 detail，但保證 code/message/request_id 不被覆寫。"""
    detail: dict[str, Any] = {}
    if extra:
        detail.update(extra)
    detail["code"] = code
    detail["message"] = message
    detail["request_id"] = request_id
    return {"detail": detail}


# ---------------------------------------------------------------------------
# Handler 實作
# ---------------------------------------------------------------------------


async def business_error_handler(request: Request, exc: BusinessError) -> JSONResponse:
    request_id = _get_request_id(request)
    if exc.http_status >= 500:
        _capture_to_sentry(
            exc, request=request, request_id=request_id, error_code=exc.code
        )
    return JSONResponse(
        status_code=exc.http_status,
        content=_envelope(
            code=exc.code,
            message=exc.message,
            request_id=request_id,
            # extra 可能帶 Decimal / datetime / set 等非 JSON 物件；過 jsonable_encoder
            # 避免 JSONResponse.render 二次拋錯變裸 500。
            extra=jsonable_encoder(exc.extra) if exc.extra else None,
        ),
        headers={"X-Request-ID": request_id},
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """HTTPException 透傳原 detail shape（保兼容 943 處 inline 與 ~200 條測試 assertion）。

    僅做三件事：
    1. status >= 500 → Sentry capture
    2. 保留 exc.headers（WWW-Authenticate / Retry-After 等）
    3. 補 X-Request-ID header（middleware 已會在 response 補；此處保險）
    """
    request_id = _get_request_id(request)
    if exc.status_code >= 500:
        _capture_to_sentry(
            exc,
            request=request,
            request_id=request_id,
            error_code=f"HTTP_{exc.status_code}",
        )
    headers = dict(exc.headers or {})
    headers.setdefault("X-Request-ID", request_id)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=headers,
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 envelope；errors list 放進 extra.errors 供前端細部顯示。Sentry 不送。"""
    request_id = _get_request_id(request)
    # exc.errors() 的 ctx 可能含 ValueError 實例 / bytes 等非 JSON 物件（Pydantic v2
    # custom validator 拋的 error）；過 jsonable_encoder 才不會在 render 二次拋 TypeError
    # → 裸 500、X-Request-ID 遺失。對齊 FastAPI 原生 handler 的做法。
    return JSONResponse(
        status_code=422,
        content=_envelope(
            code="VALIDATION_ERROR",
            message=_GENERIC_422_MESSAGE,
            request_id=request_id,
            extra={"errors": jsonable_encoder(exc.errors())},
        ),
        headers={"X-Request-ID": request_id},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """500 envelope；server 端記 stack，client 端只給安全文案。一律送 Sentry。"""
    request_id = _get_request_id(request)
    logger.error(
        "Unhandled exception on %s %s [rid=%s]: %s",
        request.method,
        request.url.path,
        request_id,
        exc,
        exc_info=True,
    )
    _capture_to_sentry(
        exc, request=request, request_id=request_id, error_code="INTERNAL_ERROR"
    )
    return JSONResponse(
        status_code=500,
        content=_envelope(
            code="INTERNAL_ERROR",
            message=_GENERIC_500_MESSAGE,
            request_id=request_id,
        ),
        headers={"X-Request-ID": request_id},
    )


def register_exception_handlers(app) -> None:
    """於 main.py 呼叫一次：把四個 handler 註冊到 FastAPI app。"""
    app.add_exception_handler(BusinessError, business_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
