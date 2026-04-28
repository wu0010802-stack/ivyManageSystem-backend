"""tests/test_cookie_samesite.py — LOW-5 SameSite=Strict 行為測試

驗證：
- 預設 SameSite=Strict
- 環境變數 COOKIE_SAMESITE=lax 可覆寫
- COOKIE_SAMESITE 為非法值會 fallback 到 strict
"""

import importlib
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _reload_cookie(env_value: str | None):
    if env_value is None:
        os.environ.pop("COOKIE_SAMESITE", None)
    else:
        os.environ["COOKIE_SAMESITE"] = env_value
    from utils import cookie

    return importlib.reload(cookie)


def _capture_set_cookie(set_token_fn) -> str:
    app = FastAPI()

    @app.get("/c")
    def _ep():
        resp = JSONResponse({"ok": True})
        set_token_fn(resp, "fake-token")
        return resp

    client = TestClient(app)
    r = client.get("/c")
    return r.headers.get("set-cookie", "")


def test_default_samesite_is_strict():
    cookie = _reload_cookie(None)
    raw = _capture_set_cookie(cookie.set_access_token_cookie)
    assert "samesite=strict" in raw.lower()


def test_env_var_lax_is_respected():
    cookie = _reload_cookie("lax")
    raw = _capture_set_cookie(cookie.set_access_token_cookie)
    assert "samesite=lax" in raw.lower()


def test_invalid_value_falls_back_to_strict(caplog):
    cookie = _reload_cookie("none")
    raw = _capture_set_cookie(cookie.set_access_token_cookie)
    assert "samesite=strict" in raw.lower()


def test_admin_token_cookie_has_same_attribute():
    cookie = _reload_cookie(None)
    raw = _capture_set_cookie(cookie.set_admin_token_cookie)
    assert "samesite=strict" in raw.lower()


@pytest.fixture(autouse=True)
def _restore_env():
    yield
    os.environ.pop("COOKIE_SAMESITE", None)
    from utils import cookie

    importlib.reload(cookie)
