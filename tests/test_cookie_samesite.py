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


def _reload_cookie():
    """settings cache 已由呼叫端 reset；重載 cookie 模組以套用新 env。"""
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


def test_default_samesite_is_strict(monkeypatch):
    monkeypatch.delenv("COOKIE_SAMESITE", raising=False)
    from config import reset_for_tests

    reset_for_tests()
    cookie = _reload_cookie()
    raw = _capture_set_cookie(cookie.set_access_token_cookie)
    assert "samesite=strict" in raw.lower()


def test_env_var_lax_is_respected(monkeypatch):
    monkeypatch.setenv("COOKIE_SAMESITE", "lax")
    from config import reset_for_tests

    reset_for_tests()
    cookie = _reload_cookie()
    raw = _capture_set_cookie(cookie.set_access_token_cookie)
    assert "samesite=lax" in raw.lower()


def test_invalid_value_falls_back_to_strict(monkeypatch, caplog):
    # 注意：'none' 在後續為跨網域部署支援後已是合法值（dev 會 fallback 到 lax），
    # 故此處改用真正不在白名單內的值來驗證 fallback 到 strict 的守衛邏輯。
    monkeypatch.setenv("COOKIE_SAMESITE", "garbage")
    from config import reset_for_tests

    reset_for_tests()
    cookie = _reload_cookie()
    raw = _capture_set_cookie(cookie.set_access_token_cookie)
    assert "samesite=strict" in raw.lower()


def test_dev_none_falls_back_to_lax(monkeypatch):
    """dev (HTTP) 環境下設 COOKIE_SAMESITE=none 會 fallback 到 lax，
    避免本機端 cookie 被瀏覽器拒收（None 強制要求 Secure）。"""
    monkeypatch.setenv("COOKIE_SAMESITE", "none")
    from config import reset_for_tests

    reset_for_tests()
    cookie = _reload_cookie()
    raw = _capture_set_cookie(cookie.set_access_token_cookie)
    assert "samesite=lax" in raw.lower()


def test_admin_token_cookie_has_same_attribute(monkeypatch):
    monkeypatch.delenv("COOKIE_SAMESITE", raising=False)
    from config import reset_for_tests

    reset_for_tests()
    cookie = _reload_cookie()
    raw = _capture_set_cookie(cookie.set_admin_token_cookie)
    assert "samesite=strict" in raw.lower()


@pytest.fixture(autouse=True)
def _restore_env():
    yield
    # monkeypatch 自動還原 env；此處只需重載 cookie 讓模組狀態回到初始
    from utils import cookie

    importlib.reload(cookie)
