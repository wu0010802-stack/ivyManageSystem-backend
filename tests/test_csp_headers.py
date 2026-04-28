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


def _build_app(reload_env: dict | None = None):
    if reload_env is not None:
        for k, v in reload_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

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
    os.environ.pop("CSP_SCRIPT_HASHES", None)
    from utils import security_headers

    importlib.reload(security_headers)


def test_csp_header_present():
    client = _build_app({"CSP_SCRIPT_HASHES": None})
    r = client.get("/x")
    assert "content-security-policy" in r.headers


def test_csp_does_not_contain_unsafe_eval():
    client = _build_app({"CSP_SCRIPT_HASHES": None})
    csp = client.get("/x").headers["content-security-policy"]
    assert "'unsafe-eval'" not in csp


def test_csp_script_src_does_not_contain_unsafe_inline_by_default():
    client = _build_app({"CSP_SCRIPT_HASHES": None})
    csp = client.get("/x").headers["content-security-policy"]
    # 解析出 script-src directive
    for directive in csp.split(";"):
        d = directive.strip()
        if d.startswith("script-src"):
            assert "'unsafe-inline'" not in d
            return
    pytest.fail("script-src directive not found in CSP")


def test_csp_style_src_still_contains_unsafe_inline():
    """style-src 'unsafe-inline' 是已知保留（Element Plus、Vue scoped style）。"""
    client = _build_app({"CSP_SCRIPT_HASHES": None})
    csp = client.get("/x").headers["content-security-policy"]
    for directive in csp.split(";"):
        d = directive.strip()
        if d.startswith("style-src"):
            assert "'unsafe-inline'" in d
            return
    pytest.fail("style-src directive not found in CSP")


def test_csp_script_src_includes_env_provided_hashes():
    client = _build_app({"CSP_SCRIPT_HASHES": "'sha256-abc123' 'sha256-def456'"})
    csp = client.get("/x").headers["content-security-policy"]
    assert "'sha256-abc123'" in csp
    assert "'sha256-def456'" in csp


def test_csp_lockdown_directives_present():
    client = _build_app({"CSP_SCRIPT_HASHES": None})
    csp = client.get("/x").headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri 'self'" in csp
    assert "form-action 'self'" in csp


def test_other_security_headers_still_set():
    client = _build_app({"CSP_SCRIPT_HASHES": None})
    h = client.get("/x").headers
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("x-frame-options") == "DENY"
    assert "strict-origin" in h.get("referrer-policy", "")
