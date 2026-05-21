"""tests/test_csp_headers.py — MEDIUM-2 CSP header 收緊驗證

驗證：
- CSP header 存在
- 不含 'unsafe-eval'
- script-src 不含 'unsafe-inline'（路徑 A 預設）
- 環境變數 CSP_SCRIPT_HASHES 可加入 sha256 hash（路徑 B fallback）
- style-src 仍保留 'unsafe-inline'（已知工程取捨）
- frame-ancestors 'none'、object-src 'none'、base-uri 'self'、form-action 'self' 仍存在
"""

import importlib
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _build_app():
    """settings cache 已由呼叫端 reset；重載 security_headers 以套用新 env。"""
    from utils import security_headers

    importlib.reload(security_headers)

    app = FastAPI()
    app.add_middleware(security_headers.SecurityHeadersMiddleware)

    @app.get("/x")
    def _ep():
        return {"ok": True}

    return TestClient(app)


@pytest.fixture(autouse=True)
def _restore_env():
    yield
    # monkeypatch 自動還原 env；此處只需重載 security_headers 讓模組狀態回到初始
    from utils import security_headers

    importlib.reload(security_headers)


def test_csp_header_present(monkeypatch):
    monkeypatch.delenv("CSP_SCRIPT_HASHES", raising=False)
    from config import reset_for_tests

    reset_for_tests()
    client = _build_app()
    r = client.get("/x")
    assert "content-security-policy" in r.headers


def test_csp_does_not_contain_unsafe_eval(monkeypatch):
    monkeypatch.delenv("CSP_SCRIPT_HASHES", raising=False)
    from config import reset_for_tests

    reset_for_tests()
    client = _build_app()
    csp = client.get("/x").headers["content-security-policy"]
    assert "'unsafe-eval'" not in csp


def test_csp_script_src_does_not_contain_unsafe_inline_by_default(monkeypatch):
    monkeypatch.delenv("CSP_SCRIPT_HASHES", raising=False)
    from config import reset_for_tests

    reset_for_tests()
    client = _build_app()
    csp = client.get("/x").headers["content-security-policy"]
    # 解析出 script-src directive
    for directive in csp.split(";"):
        d = directive.strip()
        if d.startswith("script-src"):
            assert "'unsafe-inline'" not in d
            return
    pytest.fail("script-src directive not found in CSP")


def test_csp_style_src_still_contains_unsafe_inline(monkeypatch):
    """style-src 'unsafe-inline' 是已知保留（Element Plus、Vue scoped style）。"""
    monkeypatch.delenv("CSP_SCRIPT_HASHES", raising=False)
    from config import reset_for_tests

    reset_for_tests()
    client = _build_app()
    csp = client.get("/x").headers["content-security-policy"]
    for directive in csp.split(";"):
        d = directive.strip()
        if d.startswith("style-src"):
            assert "'unsafe-inline'" in d
            return
    pytest.fail("style-src directive not found in CSP")


def test_csp_script_src_includes_env_provided_hashes(monkeypatch):
    monkeypatch.setenv("CSP_SCRIPT_HASHES", "'sha256-abc123' 'sha256-def456'")
    from config import reset_for_tests

    reset_for_tests()
    client = _build_app()
    csp = client.get("/x").headers["content-security-policy"]
    assert "'sha256-abc123'" in csp
    assert "'sha256-def456'" in csp


def test_csp_lockdown_directives_present(monkeypatch):
    monkeypatch.delenv("CSP_SCRIPT_HASHES", raising=False)
    from config import reset_for_tests

    reset_for_tests()
    client = _build_app()
    csp = client.get("/x").headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp


def test_other_security_headers_still_set(monkeypatch):
    monkeypatch.delenv("CSP_SCRIPT_HASHES", raising=False)
    from config import reset_for_tests

    reset_for_tests()
    client = _build_app()
    h = client.get("/x").headers
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("x-frame-options") == "DENY"
    assert "strict-origin" in h.get("referrer-policy", "")
