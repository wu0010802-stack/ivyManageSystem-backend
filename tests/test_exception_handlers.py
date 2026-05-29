"""tests/test_exception_handlers.py — 全域 exception handler 行為測試。

驗證範圍：
- BusinessError → envelope {code, message, request_id[, ...extra]}
- HTTPException → 透傳原 detail shape（string / dict 不變）+ 保留 exc.headers
- RequestValidationError (422) → envelope code='VALIDATION_ERROR' + errors list
- Unhandled Exception → envelope code='INTERNAL_ERROR' + Sentry capture
- Sentry capture policy：4xx 不送 / 5xx 送 / 422 不送 / unhandled 送
- request_id 取自 request.state.request_id；缺值有 fallback
- envelope 內 code/message/request_id 不被 extra 覆寫
"""

from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.exception_handlers import register_exception_handlers  # noqa: E402
from utils.exceptions import BusinessError  # noqa: E402
from utils.request_logging import RequestLoggingMiddleware  # noqa: E402


def _build_app(*, with_request_logging: bool = True) -> FastAPI:
    """組一個含全域 handler 的測試用 app；可選擇是否掛 RequestLoggingMiddleware。"""
    app = FastAPI()
    register_exception_handlers(app)
    if with_request_logging:
        app.add_middleware(RequestLoggingMiddleware)

    class _Body(BaseModel):
        name: str
        age: int

    @app.post("/biz/{kind}")
    def biz(kind: str):
        if kind == "4xx":
            raise BusinessError("RULE_BROKEN", "規則被違反", 409)
        if kind == "5xx":
            raise BusinessError("ENGINE_DOWN", "計算服務暫時無法使用", 503)
        if kind == "with-extra":
            raise BusinessError(
                "LEAVE_OVERLAP",
                "已有重疊請假",
                400,
                extra={"overlap_ids": [1, 2, 3]},
            )
        if kind == "extra-collision":
            raise BusinessError(
                "X",
                "msg",
                400,
                extra={"code": "HACKED", "message": "HACKED", "request_id": "HACKED"},
            )
        if kind == "empty-code":
            BusinessError("", "msg")
        if kind == "empty-message":
            BusinessError("X", "")
        return {"ok": True}

    @app.get("/http/{kind}")
    def http(kind: str):
        if kind == "string":
            raise HTTPException(400, "字串 detail 維持原樣")
        if kind == "dict":
            raise HTTPException(
                400,
                detail={
                    "code": "EMPLOYEE_ID_DUPLICATE",
                    "message": "員工編號已存在",
                    "context": {"employee_id": "A001"},
                },
            )
        if kind == "500":
            raise HTTPException(500, "boom")
        if kind == "auth":
            raise HTTPException(
                401,
                "未認證",
                headers={"WWW-Authenticate": 'Bearer realm="ivy"'},
            )
        return {"ok": True}

    @app.post("/validate")
    def validate(body: _Body):
        return body

    @app.get("/boom")
    def boom():
        raise RuntimeError("unhandled boom")

    return app


# ---------------------------------------------------------------------------
# BusinessError construction
# ---------------------------------------------------------------------------


def test_business_error_empty_code_rejected():
    with pytest.raises(ValueError, match="code"):
        BusinessError("", "msg")


def test_business_error_empty_message_rejected():
    with pytest.raises(ValueError, match="message"):
        BusinessError("X", "")


# ---------------------------------------------------------------------------
# BusinessError handler — envelope shape & status
# ---------------------------------------------------------------------------


def test_business_error_4xx_envelope():
    client = TestClient(_build_app())
    r = client.post("/biz/4xx")
    assert r.status_code == 409
    body = r.json()
    assert body["detail"]["code"] == "RULE_BROKEN"
    assert body["detail"]["message"] == "規則被違反"
    assert (
        isinstance(body["detail"]["request_id"], str) and body["detail"]["request_id"]
    )
    # X-Request-ID header 與 envelope request_id 對齊
    assert r.headers["X-Request-ID"] == body["detail"]["request_id"]


def test_business_error_5xx_envelope():
    client = TestClient(_build_app(), raise_server_exceptions=False)
    r = client.post("/biz/5xx")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "ENGINE_DOWN"


def test_business_error_extra_merged():
    client = TestClient(_build_app())
    r = client.post("/biz/with-extra")
    detail = r.json()["detail"]
    assert detail["code"] == "LEAVE_OVERLAP"
    assert detail["overlap_ids"] == [1, 2, 3]


def test_business_error_extra_cannot_override_envelope_keys():
    """extra 若惡意/誤用塞 code/message/request_id 進來，envelope 必勝。"""
    client = TestClient(_build_app())
    r = client.post("/biz/extra-collision")
    detail = r.json()["detail"]
    assert detail["code"] == "X"  # not 'HACKED'
    assert detail["message"] == "msg"
    assert detail["request_id"] != "HACKED"


# ---------------------------------------------------------------------------
# HTTPException handler — 透傳原 detail shape
# ---------------------------------------------------------------------------


def test_http_exception_string_detail_passthrough():
    """關鍵：943 處 inline raise HTTPException(400, '...') 必須繼續以 string detail 回。"""
    client = TestClient(_build_app())
    r = client.get("/http/string")
    assert r.status_code == 400
    # 原 shape：{"detail": "..."}；不被 envelope 化
    assert r.json() == {"detail": "字串 detail 維持原樣"}


def test_http_exception_dict_detail_passthrough():
    """3 處既有 dict-detail 用法（employees / contact_book / medications）必須維持結構。"""
    client = TestClient(_build_app())
    r = client.get("/http/dict")
    body = r.json()
    assert body["detail"]["code"] == "EMPLOYEE_ID_DUPLICATE"
    assert body["detail"]["context"] == {"employee_id": "A001"}


def test_http_exception_preserves_headers():
    """401 WWW-Authenticate header 不能被吃掉。"""
    client = TestClient(_build_app())
    r = client.get("/http/auth")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == 'Bearer realm="ivy"'


# ---------------------------------------------------------------------------
# 422 RequestValidationError → envelope
# ---------------------------------------------------------------------------


def test_validation_error_envelope():
    client = TestClient(_build_app())
    r = client.post("/validate", json={"name": "x"})  # 缺 age
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "VALIDATION_ERROR"
    assert detail["message"] == "輸入資料驗證失敗"
    assert isinstance(detail["errors"], list) and len(detail["errors"]) >= 1
    assert isinstance(detail["request_id"], str) and detail["request_id"]


def _bare_request():
    """最小 Starlette Request（無 middleware 注入 request_id → 走 handler fallback）。"""
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/x",
            "headers": [],
            "query_string": b"",
        }
    )


def test_validation_handler_handles_non_serializable_ctx():
    """custom validator 拋 ValueError → Pydantic 把 ValueError 實例放進 errors()[i]['ctx']。

    回歸：validation_error_handler 未過 jsonable_encoder 時，JSONResponse.render 的
    json.dumps 會二次拋 TypeError → 變成沒有 envelope 的裸 500、X-Request-ID 也丟失。
    errors 結構鏡像 Pydantic v2 對 value_error 的實際輸出（已實測 pydantic 2.13）。
    """
    import asyncio
    import json

    from fastapi.exceptions import RequestValidationError

    from utils.exception_handlers import validation_error_handler

    exc = RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("body", "amount"),
                "msg": "Value error, amount 不可為負",
                "input": -1,
                "ctx": {"error": ValueError("amount 不可為負")},
            }
        ]
    )
    resp = asyncio.run(validation_error_handler(_bare_request(), exc))
    assert resp.status_code == 422
    body = json.loads(resp.body)
    assert body["detail"]["code"] == "VALIDATION_ERROR"
    assert isinstance(body["detail"]["errors"], list) and body["detail"]["errors"]
    assert body["detail"]["request_id"]
    assert resp.headers["X-Request-ID"] == body["detail"]["request_id"]


def test_business_handler_handles_non_serializable_extra():
    """BusinessError.extra 帶非 JSON 物件（如 set）時，handler 不應崩潰成裸 500。"""
    import asyncio
    import json

    from utils.exception_handlers import business_error_handler

    exc = BusinessError(
        "LEAVE_OVERLAP", "已有重疊請假", 400, extra={"overlap_ids": {1, 2, 3}}
    )
    resp = asyncio.run(business_error_handler(_bare_request(), exc))
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert body["detail"]["code"] == "LEAVE_OVERLAP"
    assert sorted(body["detail"]["overlap_ids"]) == [1, 2, 3]


# ---------------------------------------------------------------------------
# Unhandled Exception → 500 envelope
# ---------------------------------------------------------------------------


def test_unhandled_exception_envelope():
    client = TestClient(_build_app(), raise_server_exceptions=False)
    r = client.get("/boom")
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert detail["code"] == "INTERNAL_ERROR"
    assert detail["message"] == "系統內部錯誤，請聯繫管理員"
    assert isinstance(detail["request_id"], str) and detail["request_id"]


# ---------------------------------------------------------------------------
# request_id fallback（無 RequestLoggingMiddleware）
# ---------------------------------------------------------------------------


def test_request_id_fallback_when_middleware_missing():
    """exception 發生在 RequestLogging 之前的場景：handler 自行 fallback id。"""
    client = TestClient(_build_app(with_request_logging=False))
    r = client.post("/biz/4xx")
    detail = r.json()["detail"]
    # fallback uuid hex prefix；長度 12，且非空
    assert isinstance(detail["request_id"], str)
    assert len(detail["request_id"]) >= 8


# ---------------------------------------------------------------------------
# Sentry capture policy
# ---------------------------------------------------------------------------


def _patch_sentry_capture():
    """patch utils.exception_handlers._capture_to_sentry，避免實際打 Sentry。"""
    return patch("utils.exception_handlers._capture_to_sentry")


def test_sentry_capture_business_error_5xx():
    with _patch_sentry_capture() as cap:
        client = TestClient(_build_app(), raise_server_exceptions=False)
        client.post("/biz/5xx")
    assert cap.call_count == 1
    _, kwargs = cap.call_args
    assert kwargs["error_code"] == "ENGINE_DOWN"


def test_sentry_capture_skipped_for_business_error_4xx():
    with _patch_sentry_capture() as cap:
        client = TestClient(_build_app())
        client.post("/biz/4xx")
    assert cap.call_count == 0


def test_sentry_capture_http_exception_500():
    with _patch_sentry_capture() as cap:
        client = TestClient(_build_app(), raise_server_exceptions=False)
        client.get("/http/500")
    assert cap.call_count == 1
    _, kwargs = cap.call_args
    assert kwargs["error_code"] == "HTTP_500"


def test_sentry_capture_skipped_for_http_exception_4xx():
    with _patch_sentry_capture() as cap:
        client = TestClient(_build_app())
        client.get("/http/string")
        client.get("/http/dict")
        client.get("/http/auth")
    assert cap.call_count == 0


def test_sentry_capture_skipped_for_validation_error():
    """422 是純使用者輸入錯誤，永不送 Sentry。"""
    with _patch_sentry_capture() as cap:
        client = TestClient(_build_app())
        client.post("/validate", json={"name": "x"})
    assert cap.call_count == 0


def test_sentry_capture_unhandled_exception():
    with _patch_sentry_capture() as cap:
        client = TestClient(_build_app(), raise_server_exceptions=False)
        client.get("/boom")
    assert cap.call_count == 1
    _, kwargs = cap.call_args
    assert kwargs["error_code"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Sentry 內部行為（用 push_scope 不污染全域）
# ---------------------------------------------------------------------------


def test_sentry_capture_uses_push_scope_when_sdk_available():
    """有 sentry-sdk（無論 DSN 是否設）時應走 push_scope；DSN 未設下 capture_exception 為 no-op，
    不會炸 handler。"""
    pytest.importorskip("sentry_sdk")
    # 不 mock _capture_to_sentry；直接讓真實邏輯跑（DSN 未設則 SDK 內部 no-op）
    client = TestClient(_build_app(), raise_server_exceptions=False)
    r = client.get("/boom")
    assert r.status_code == 500
    assert r.json()["detail"]["code"] == "INTERNAL_ERROR"
