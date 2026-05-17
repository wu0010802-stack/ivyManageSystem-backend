"""tests/test_mcp_activity_crud_client.py — IvyApiClient 行為測試。

用 httpx.MockTransport 攔截所有出向 request，不需後端真實跑起來。
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable

import httpx
import pytest

from mcp_server.activity_crud.client import IvyApiClient, IvyApiError

# ── 公用 helpers ─────────────────────────────────────────────────────────


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> IvyApiClient:
    """建一個用 MockTransport 攔截 request 的 IvyApiClient。"""
    return IvyApiClient(
        base_url="http://test",
        username="mcp-bot",
        password="secret",
        transport=httpx.MockTransport(handler),
    )


def _login_ok(request: httpx.Request) -> httpx.Response:
    """200 + set-cookie；模擬 ivy-backend 登入成功。"""
    return httpx.Response(
        200,
        headers={"set-cookie": "access_token=fake-jwt; Path=/api"},
        json={"user": {"username": "mcp-bot"}},
    )


# ── 測試 ─────────────────────────────────────────────────────────────────


def test_request_logs_in_then_returns_json():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/api/auth/login":
            return _login_ok(request)
        if request.url.path == "/api/activity/courses":
            return httpx.Response(200, json={"courses": [], "total": 0})
        return httpx.Response(404, json={"detail": "not found"})

    async def go():
        client = _mock_client(handler)
        try:
            data = await client.request("GET", "/api/activity/courses")
        finally:
            await client.aclose()
        return data

    data = asyncio.run(go())
    assert data == {"courses": [], "total": 0}
    # login 先、然後才是 GET courses
    assert calls == ["POST /api/auth/login", "GET /api/activity/courses"]


def test_login_failure_raises_with_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(401, json={"detail": "帳號或密碼錯誤"})
        raise AssertionError("不該打到別的 endpoint")

    async def go():
        client = _mock_client(handler)
        try:
            with pytest.raises(IvyApiError) as exc_info:
                await client.request("GET", "/api/activity/courses")
            return exc_info.value
        finally:
            await client.aclose()

    err = asyncio.run(go())
    assert err.status == 401
    assert "帳號或密碼錯誤" in err.message


def test_401_triggers_refresh_then_retries():
    state = {"login_count": 0, "courses_count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            state["login_count"] += 1
            return _login_ok(request)
        if request.url.path == "/api/activity/courses":
            state["courses_count"] += 1
            # 第一次 401（過期）；第二次（refresh 後）200
            if state["courses_count"] == 1:
                return httpx.Response(401, json={"detail": "Token 已失效"})
            return httpx.Response(200, json={"courses": [], "total": 0})
        return httpx.Response(404)

    async def go():
        client = _mock_client(handler)
        try:
            return await client.request("GET", "/api/activity/courses")
        finally:
            await client.aclose()

    data = asyncio.run(go())
    assert data == {"courses": [], "total": 0}
    assert state["login_count"] == 2  # 初始 + refresh
    assert state["courses_count"] == 2  # 第一次 401，refresh 後重打


def test_401_persistent_raises_after_one_refresh():
    state = {"login_count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            state["login_count"] += 1
            return _login_ok(request)
        # 持續 401（例如帳號被停用、token_version 變了）
        return httpx.Response(401, json={"detail": "Token 已失效"})

    async def go():
        client = _mock_client(handler)
        try:
            with pytest.raises(IvyApiError) as exc_info:
                await client.request("GET", "/api/activity/courses")
            return exc_info.value
        finally:
            await client.aclose()

    err = asyncio.run(go())
    assert err.status == 401
    # 只 refresh 一次，避免無限迴圈
    assert state["login_count"] == 2


def test_4xx_passthrough_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return _login_ok(request)
        return httpx.Response(400, json={"detail": "課程「美術」已存在"})

    async def go():
        client = _mock_client(handler)
        try:
            with pytest.raises(IvyApiError) as exc_info:
                await client.request(
                    "POST", "/api/activity/courses", json={"name": "美術"}
                )
            return exc_info.value
        finally:
            await client.aclose()

    err = asyncio.run(go())
    assert err.status == 400
    assert err.message == "課程「美術」已存在"


def test_422_validation_detail_flattened():
    """FastAPI 422 detail 是 list，要被攤平成可讀字串。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return _login_ok(request)
        return httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "loc": ["body", "price"],
                        "msg": "ensure this value is >= 0",
                        "type": "value_error.number.not_ge",
                    },
                    {
                        "loc": ["body", "name"],
                        "msg": "field required",
                        "type": "value_error.missing",
                    },
                ]
            },
        )

    async def go():
        client = _mock_client(handler)
        try:
            with pytest.raises(IvyApiError) as exc_info:
                await client.request("POST", "/api/activity/courses", json={})
            return exc_info.value
        finally:
            await client.aclose()

    err = asyncio.run(go())
    assert err.status == 422
    assert "body.price" in err.message
    assert "body.name" in err.message
    assert "field required" in err.message


def test_network_failure_wrapped_as_500():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async def go():
        client = _mock_client(handler)
        try:
            with pytest.raises(IvyApiError) as exc_info:
                await client.request("GET", "/api/activity/courses")
            return exc_info.value
        finally:
            await client.aclose()

    err = asyncio.run(go())
    assert err.status == 500
    assert "無法連線" in err.message or "連線失敗" in err.message


def test_missing_env_raises_at_construct_time(monkeypatch):
    """env 沒設 → ctor 立刻 raise，不會延後到 first call。"""
    monkeypatch.delenv("IVY_MCP_USERNAME", raising=False)
    monkeypatch.delenv("IVY_MCP_PASSWORD", raising=False)
    with pytest.raises(IvyApiError) as exc_info:
        IvyApiClient(base_url="http://test")
    assert "IVY_MCP_USERNAME" in exc_info.value.message


def test_request_carries_params_and_json():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return _login_ok(request)
        captured["query"] = dict(request.url.params)
        captured["body"] = (
            json.loads(request.content.decode()) if request.content else None
        )
        return httpx.Response(200, json={"ok": True})

    async def go():
        client = _mock_client(handler)
        try:
            return await client.request(
                "POST",
                "/api/activity/courses",
                params={"school_year": "114", "semester": "2"},
                json={"name": "美術", "price": 1500},
            )
        finally:
            await client.aclose()

    asyncio.run(go())
    assert captured["query"] == {"school_year": "114", "semester": "2"}
    assert captured["body"] == {"name": "美術", "price": 1500}


def test_204_no_content_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return _login_ok(request)
        return httpx.Response(204)

    async def go():
        client = _mock_client(handler)
        try:
            return await client.request("DELETE", "/api/activity/courses/1")
        finally:
            await client.aclose()

    assert asyncio.run(go()) is None
