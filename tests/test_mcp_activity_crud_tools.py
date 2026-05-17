"""tests/test_mcp_activity_crud_tools.py — 12 個 tool 行為測試。

驗證每個 tool 對應正確的 REST endpoint + method + body。
透過 httpx.MockTransport 攔截實際出向 HTTP，確保 URL 拼接、param 篩 None、JSON body 正確。
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable

import httpx
import pytest

from mcp.server.fastmcp import FastMCP

from mcp_server.activity_crud.client import IvyApiClient
from mcp_server.activity_crud.tools import register_tools

# ── 共用 fixture：建好已註冊 12 個 tool 的 mcp + 攔截器 ──────────────────


def _build(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[FastMCP, IvyApiClient]:
    client = IvyApiClient(
        base_url="http://test",
        username="mcp-bot",
        password="secret",
        transport=httpx.MockTransport(handler),
    )
    mcp = FastMCP("test")
    register_tools(mcp, client)
    return mcp, client


def _login_ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"set-cookie": "access_token=fake-jwt; Path=/api"},
        json={"user": {"username": "mcp-bot"}},
    )


def _make_handler(
    responder: Callable[[httpx.Request], httpx.Response],
) -> Callable[[httpx.Request], httpx.Response]:
    """把 login 自動處理掉，剩下交給 responder。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return _login_ok(request)
        return responder(request)

    return handler


def _run_tool(mcp: FastMCP, client: IvyApiClient, name: str, args: dict):
    async def go():
        try:
            return await mcp.call_tool(name, args)
        finally:
            await client.aclose()

    return asyncio.run(go())


# ── 課程 8 個 tool happy path ───────────────────────────────────────────


def test_list_courses_hits_get_endpoint_with_params():
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"courses": [], "total": 0})

    mcp, client = _build(_make_handler(respond))
    _run_tool(
        mcp,
        client,
        "list_courses",
        {"school_year": 114, "semester": 2, "skip": 0, "limit": 50},
    )
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/activity/courses"
    assert captured["params"] == {
        "school_year": "114",
        "semester": "2",
        "skip": "0",
        "limit": "50",
    }


def test_list_courses_omits_none_school_year():
    """school_year=None 時不該帶到 query string。"""
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"courses": []})

    mcp, client = _build(_make_handler(respond))
    _run_tool(mcp, client, "list_courses", {})
    # skip/limit 有預設值，但 school_year/semester 不該出現
    assert "school_year" not in captured["params"]
    assert "semester" not in captured["params"]


def test_get_course_hits_path_with_id():
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"id": 42, "name": "美術"})

    mcp, client = _build(_make_handler(respond))
    _run_tool(mcp, client, "get_course", {"course_id": 42})
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/activity/courses/42"


def test_create_course_posts_with_full_body():
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"id": 99, "message": "課程新增成功"})

    mcp, client = _build(_make_handler(respond))
    _run_tool(
        mcp,
        client,
        "create_course",
        {
            "name": "繪本英語",
            "price": 2000,
            "sessions": 12,
            "capacity": 15,
            "school_year": 114,
            "semester": 2,
            "min_age_months": 36,
            "max_age_months": 60,
            "meeting_weekday": 2,
            "meeting_start_time": "16:00",
            "meeting_end_time": "17:00",
        },
    )
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/activity/courses"
    body = captured["body"]
    assert body["name"] == "繪本英語"
    assert body["price"] == 2000
    assert body["sessions"] == 12
    assert body["min_age_months"] == 36
    assert body["meeting_start_time"] == "16:00"
    assert body["meeting_end_time"] == "17:00"
    # 沒給的欄位不該出現（除了有預設值的 capacity/allow_waitlist）
    assert "video_url" not in body
    assert "description" not in body


def test_create_course_omits_none_optional_fields():
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"id": 1, "message": "ok"})

    mcp, client = _build(_make_handler(respond))
    _run_tool(mcp, client, "create_course", {"name": "舞蹈", "price": 1500})
    body = captured["body"]
    assert body["name"] == "舞蹈"
    assert body["price"] == 1500
    # 預設值有的會出現（capacity=30, allow_waitlist=True）
    assert body["capacity"] == 30
    assert body["allow_waitlist"] is True
    # Optional[None] 全部過濾掉
    for field in (
        "sessions",
        "video_url",
        "description",
        "school_year",
        "semester",
        "min_age_months",
        "max_age_months",
        "meeting_weekday",
        "meeting_start_time",
        "meeting_end_time",
    ):
        assert field not in body, f"{field} 不該出現在 body"


def test_copy_courses_posts_full_body():
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"copied": 5})

    mcp, client = _build(_make_handler(respond))
    _run_tool(
        mcp,
        client,
        "copy_courses_from_previous",
        {
            "source_school_year": 114,
            "source_semester": 1,
            "target_school_year": 114,
            "target_semester": 2,
        },
    )
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/activity/courses/copy-from-previous"
    assert captured["body"] == {
        "source_school_year": 114,
        "source_semester": 1,
        "target_school_year": 114,
        "target_semester": 2,
    }


def test_update_course_puts_only_provided_fields():
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"message": "課程更新成功"})

    mcp, client = _build(_make_handler(respond))
    _run_tool(
        mcp, client, "update_course", {"course_id": 42, "price": 1800, "capacity": 20}
    )
    assert captured["method"] == "PUT"
    assert captured["path"] == "/api/activity/courses/42"
    assert captured["body"] == {"price": 1800, "capacity": 20}


def test_delete_course_hits_delete_path():
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"message": "課程已停用"})

    mcp, client = _build(_make_handler(respond))
    _run_tool(mcp, client, "delete_course", {"course_id": 7})
    assert captured["method"] == "DELETE"
    assert captured["path"] == "/api/activity/courses/7"


def test_list_course_waitlist_hits_nested_path():
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"waitlist": []})

    mcp, client = _build(_make_handler(respond))
    _run_tool(mcp, client, "list_course_waitlist", {"course_id": 7})
    assert captured["path"] == "/api/activity/courses/7/waitlist"


def test_list_course_enrolled_hits_nested_path():
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={"enrolled": []})

    mcp, client = _build(_make_handler(respond))
    _run_tool(mcp, client, "list_course_enrolled", {"course_id": 7})
    assert captured["path"] == "/api/activity/courses/7/enrolled"


# ── 用品 4 個 tool happy path ───────────────────────────────────────────


def test_list_supplies_hits_get_with_params():
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"supplies": [], "total": 0})

    mcp, client = _build(_make_handler(respond))
    _run_tool(mcp, client, "list_supplies", {"school_year": 114, "semester": 2})
    assert captured["path"] == "/api/activity/supplies"
    assert captured["params"]["school_year"] == "114"
    assert captured["params"]["semester"] == "2"


def test_create_supply_posts_body():
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(201, json={"id": 11, "message": "用品新增成功"})

    mcp, client = _build(_make_handler(respond))
    _run_tool(mcp, client, "create_supply", {"name": "畫筆", "price": 80})
    assert captured["method"] == "POST"
    assert captured["body"] == {"name": "畫筆", "price": 80}


def test_update_supply_puts_only_provided_fields():
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"message": "用品更新成功"})

    mcp, client = _build(_make_handler(respond))
    _run_tool(mcp, client, "update_supply", {"supply_id": 11, "price": 100})
    assert captured["path"] == "/api/activity/supplies/11"
    assert captured["body"] == {"price": 100}


def test_delete_supply_hits_delete_path():
    captured: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"message": "用品已停用"})

    mcp, client = _build(_make_handler(respond))
    _run_tool(mcp, client, "delete_supply", {"supply_id": 11})
    assert captured["method"] == "DELETE"
    assert captured["path"] == "/api/activity/supplies/11"


# ── Error path：後端 4xx 透傳成 MCP isError ─────────────────────────────


def test_create_course_4xx_surface_chinese_detail():
    """後端回 400「課程「美術」已存在」應該透傳給 LLM。"""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "課程「美術」已存在"})

    mcp, client = _build(_make_handler(respond))

    async def go():
        try:
            with pytest.raises(Exception) as exc_info:
                await mcp.call_tool("create_course", {"name": "美術", "price": 1500})
            return exc_info.value
        finally:
            await client.aclose()

    err = asyncio.run(go())
    # MCP 會把 RuntimeError 包成 ToolError 或類似；訊息要看得到後端 detail
    assert "課程「美術」已存在" in str(err)


def test_update_course_404_surface_message():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "找不到課程"})

    mcp, client = _build(_make_handler(respond))

    async def go():
        try:
            with pytest.raises(Exception) as exc_info:
                await mcp.call_tool("update_course", {"course_id": 999, "name": "X"})
            return exc_info.value
        finally:
            await client.aclose()

    err = asyncio.run(go())
    assert "找不到課程" in str(err)


def test_create_supply_403_permission_message():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "權限不足：缺少 ACTIVITY_WRITE"})

    mcp, client = _build(_make_handler(respond))

    async def go():
        try:
            with pytest.raises(Exception) as exc_info:
                await mcp.call_tool("create_supply", {"name": "彩色筆", "price": 60})
            return exc_info.value
        finally:
            await client.aclose()

    err = asyncio.run(go())
    assert "ACTIVITY_WRITE" in str(err) or "權限不足" in str(err)


def test_delete_course_422_validation_message():
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "loc": ["path", "course_id"],
                        "msg": "ensure this value is greater than 0",
                    }
                ]
            },
        )

    mcp, client = _build(_make_handler(respond))

    async def go():
        try:
            with pytest.raises(Exception) as exc_info:
                await mcp.call_tool("delete_course", {"course_id": -1})
            return exc_info.value
        finally:
            await client.aclose()

    err = asyncio.run(go())
    assert "course_id" in str(err)
