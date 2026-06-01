"""
唯讀模擬守衛測試。

使用最小化 FastAPI app（不 import main.app）並掛 ReadonlyImpersonationMiddleware，
測試所有 token 模式組合的攔截行為。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from utils.auth import create_access_token

# ─── 最小化 app fixture ─────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def guarded_app():
    """掛上 ReadonlyImpersonationMiddleware 的最小化 app（不需 DB）。"""
    from utils.readonly_guard import ReadonlyImpersonationMiddleware

    app = FastAPI()
    app.add_middleware(ReadonlyImpersonationMiddleware)

    # 幾個 stub route 讓「放行」測試確實打到 handler，回傳 200/201 而非 404
    @app.get("/api/portal/home/summary")
    async def portal_summary():
        return {"ok": True}

    @app.post("/api/portal/my-overtimes")
    async def portal_post_overtime():
        return JSONResponse({"ok": True}, status_code=201)

    @app.post("/api/auth/end-impersonate")
    async def end_impersonate():
        return {"ok": True}

    @app.post("/api/employees")
    async def create_employee():
        return JSONResponse({"ok": True}, status_code=201)

    return app


@pytest.fixture(scope="module")
def client(guarded_app):
    with TestClient(guarded_app) as c:
        yield c


# ─── token fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def readonly_impersonation_cookie():
    """readonly 模擬 token（老師視角，admin 發起）。"""
    return create_access_token(
        {
            "user_id": 10,
            "employee_id": 10,
            "role": "teacher",
            "name": "老師甲",
            "impersonated_by": 1,
            "impersonated_by_name": "系統管理員",
            "impersonation_mode": "readonly",
        }
    )


@pytest.fixture(scope="module")
def write_impersonation_cookie():
    """write 模擬 token（老師視角，admin 發起）。"""
    return create_access_token(
        {
            "user_id": 10,
            "employee_id": 10,
            "role": "teacher",
            "name": "老師甲",
            "impersonated_by": 1,
            "impersonated_by_name": "系統管理員",
            "impersonation_mode": "write",
        }
    )


@pytest.fixture(scope="module")
def normal_cookie():
    """一般登入 token（無 impersonation_mode claim）。"""
    return create_access_token(
        {
            "user_id": 10,
            "employee_id": 10,
            "role": "teacher",
            "name": "老師甲",
        }
    )


# ─── 測試 ───────────────────────────────────────────────────────────────────


def test_readonly_blocks_portal_write(client, readonly_impersonation_cookie):
    """readonly 守衛在 routing 前攔截 POST /api/portal/my-overtimes → 403 + 唯讀。"""
    resp = client.post(
        "/api/portal/my-overtimes",
        json={},
        cookies={"access_token": readonly_impersonation_cookie},
    )
    assert resp.status_code == 403
    assert "唯讀" in resp.json()["detail"]


def test_readonly_blocks_nonportal_write(client, readonly_impersonation_cookie):
    """readonly 守衛阻擋 non-portal 寫入（POST /api/employees）→ 403。"""
    resp = client.post(
        "/api/employees",
        json={},
        cookies={"access_token": readonly_impersonation_cookie},
    )
    assert resp.status_code == 403


def test_readonly_allows_get(client, readonly_impersonation_cookie):
    """readonly token 對 GET 請求放行（非 mutating method）。"""
    resp = client.get(
        "/api/portal/home/summary",
        cookies={"access_token": readonly_impersonation_cookie},
    )
    assert resp.status_code != 403


def test_readonly_allows_end_impersonate(client, readonly_impersonation_cookie):
    """readonly token 對 POST /api/auth/end-impersonate 放行（exempt path）。"""
    resp = client.post(
        "/api/auth/end-impersonate",
        cookies={"access_token": readonly_impersonation_cookie},
    )
    assert resp.status_code != 403


def test_write_mode_not_blocked(client, write_impersonation_cookie):
    """write 模擬 token 的 GET 請求放行。"""
    resp = client.get(
        "/api/portal/home/summary",
        cookies={"access_token": write_impersonation_cookie},
    )
    assert resp.status_code != 403


def test_write_mode_post_not_blocked(client, write_impersonation_cookie):
    """write 模擬 token 的 POST 寫入請求不被攔截（這是 dual-mode 核心契約）。"""
    resp = client.post(
        "/api/portal/my-overtimes",
        json={},
        cookies={"access_token": write_impersonation_cookie},
    )
    assert resp.status_code != 403


def test_normal_token_post_not_blocked(client, normal_cookie):
    """一般登入 token（無 impersonation_mode）的 POST 不被攔截。"""
    resp = client.post(
        "/api/portal/my-overtimes",
        json={},
        cookies={"access_token": normal_cookie},
    )
    assert resp.status_code != 403
