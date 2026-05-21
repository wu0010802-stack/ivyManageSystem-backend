"""tests/test_request_logging.py — request_id middleware 行為驗證。

驗證範圍：
- RequestIdLogFilter 注入 ContextVar 值；缺 context 時填預設 "-"
- middleware 接受外部 X-Request-ID header；缺則自產
- middleware 內 request handler 透過 ContextVar 可拿到 request_id
- middleware 呼叫 sentry_sdk.set_tag（即便 DSN 未設也安全執行）
- request 結束後 ContextVar 復原（不污染下一個 request 或 background task）
"""

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils.request_logging import (
    RequestIdLogFilter,
    RequestLoggingMiddleware,
    request_id_var,
)

# ---------------------------------------------------------------------------
# RequestIdLogFilter
# ---------------------------------------------------------------------------


class TestRequestIdLogFilter:
    def test_injects_default_when_no_context(self):
        rec = logging.LogRecord("test", logging.INFO, __file__, 0, "msg", None, None)
        assert RequestIdLogFilter().filter(rec) is True
        assert rec.request_id == "-"

    def test_injects_contextvar_value(self):
        token = request_id_var.set("abc123def456")
        try:
            rec = logging.LogRecord(
                "test", logging.INFO, __file__, 0, "msg", None, None
            )
            RequestIdLogFilter().filter(rec)
            assert rec.request_id == "abc123def456"
        finally:
            request_id_var.reset(token)

    def test_formatter_renders_request_id_field(self):
        """formatter `%(request_id)s` 應從 filter 注入的 attr 拿到值。"""
        token = request_id_var.set("xyz789")
        try:
            rec = logging.LogRecord(
                "test", logging.INFO, __file__, 0, "ping", None, None
            )
            RequestIdLogFilter().filter(rec)
            fmt = logging.Formatter("[rid=%(request_id)s] %(message)s")
            assert fmt.format(rec) == "[rid=xyz789] ping"
        finally:
            request_id_var.reset(token)


# ---------------------------------------------------------------------------
# RequestLoggingMiddleware
# ---------------------------------------------------------------------------


def _build_app(captured: dict):
    """組一個只掛 RequestLoggingMiddleware 的最小 FastAPI app。

    用 captured dict 帶出 handler 內看到的 ContextVar 值，給 assertion 用。
    """
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/ping")
    def ping():
        captured["rid_in_handler"] = request_id_var.get()
        return {"ok": True}

    return app


class TestRequestLoggingMiddleware:
    def test_generates_request_id_when_header_missing(self):
        captured: dict = {}
        client = TestClient(_build_app(captured))
        r = client.get("/ping")
        assert r.status_code == 200
        rid = r.headers["X-Request-ID"]
        assert rid and rid != "-"
        # handler 看到的 ContextVar 與 response header 一致
        assert captured["rid_in_handler"] == rid

    def test_honours_inbound_request_id_header(self):
        captured: dict = {}
        client = TestClient(_build_app(captured))
        r = client.get("/ping", headers={"X-Request-ID": "client-supplied-id-001"})
        assert r.headers["X-Request-ID"] == "client-supplied-id-001"
        assert captured["rid_in_handler"] == "client-supplied-id-001"

    def test_contextvar_restored_after_request(self):
        """request 結束後 ContextVar 不殘留（避免污染下一個 request / 背景任務）。"""
        captured: dict = {}
        client = TestClient(_build_app(captured))
        client.get("/ping")
        # TestClient 跑在獨立 thread/loop；本 thread 的 ContextVar 仍是預設
        assert request_id_var.get() == "-"

    def test_sentry_set_tag_called(self, monkeypatch):
        """middleware 應呼叫 sentry_sdk.set_tag('request_id', <rid>)，無論 DSN 是否設定。"""
        import sentry_sdk

        recorded: list[tuple[str, str]] = []

        def _fake_set_tag(key, value):
            recorded.append((key, value))

        monkeypatch.setattr(sentry_sdk, "set_tag", _fake_set_tag)

        captured: dict = {}
        client = TestClient(_build_app(captured))
        r = client.get("/ping", headers={"X-Request-ID": "rid-sentry-tag-test"})

        # 確認 tag 被設定，值與最終 response header 一致
        assert ("request_id", "rid-sentry-tag-test") in recorded
        assert r.headers["X-Request-ID"] == "rid-sentry-tag-test"

    def test_response_time_header_set(self):
        captured: dict = {}
        client = TestClient(_build_app(captured))
        r = client.get("/ping")
        assert "X-Response-Time" in r.headers
        assert r.headers["X-Response-Time"].endswith("ms")
