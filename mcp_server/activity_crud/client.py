"""IvyApiClient — MCP server 用 httpx async client 包裝。

設計：
- 後端 `/api/auth/login` 把 JWT 寫在 httpOnly cookie（`access_token`，path=/api）。
- httpx.AsyncClient 自帶 cookie jar，登入後自動把 cookie 帶在後續 request。
- 收到 401 → 清 cookie → 重新 login 一次 → retry；第二次仍 401 直接拋。

env：
  IVY_API_BASE_URL  預設 http://localhost:8088
  IVY_MCP_USERNAME  必填
  IVY_MCP_PASSWORD  必填
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx


class IvyApiError(Exception):
    """後端 4xx/5xx 或網路錯誤的統一表達。"""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}")


class IvyApiClient:
    """連 ivy-backend FastAPI 的 async client，自動管理登入 cookie。

    使用方式：

        async with IvyApiClient() as client:
            data = await client.request("GET", "/api/activity/courses")
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 30.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.base_url = base_url or os.environ.get(
            "IVY_API_BASE_URL", "http://localhost:8088"
        )
        self.username = username or os.environ.get("IVY_MCP_USERNAME") or ""
        self.password = password or os.environ.get("IVY_MCP_PASSWORD") or ""
        if not self.username or not self.password:
            raise IvyApiError(
                500,
                "IVY_MCP_USERNAME / IVY_MCP_PASSWORD 未設定，無法登入 ivy-backend",
            )

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
        )
        self._logged_in = False

    async def __aenter__(self) -> "IvyApiClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _login(self) -> None:
        """POST /api/auth/login；cookie 由 httpx jar 自動 capture。"""
        try:
            resp = await self._client.post(
                "/api/auth/login",
                json={"username": self.username, "password": self.password},
            )
        except httpx.HTTPError as exc:
            raise IvyApiError(
                500,
                f"無法連線 ivy-backend ({self.base_url})：{exc.__class__.__name__}",
            ) from exc

        if resp.status_code != 200:
            detail = _extract_detail(resp) or "登入失敗"
            raise IvyApiError(
                resp.status_code,
                f"MCP 帳號登入失敗：{detail}（檢查 IVY_MCP_USERNAME/PASSWORD）",
            )
        self._logged_in = True

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
    ) -> Any:
        """打後端 REST endpoint；401 自動 refresh 重打一次。

        Args:
            method: HTTP method（GET/POST/PUT/DELETE）
            path: e.g. "/api/activity/courses"
            params: query string
            json: request body

        Returns:
            後端 JSON body（dict / list）。204 回 None。

        Raises:
            IvyApiError: 4xx/5xx 或網路錯誤。
        """
        if not self._logged_in:
            await self._login()

        resp = await self._send(method, path, params=params, json=json)
        if resp.status_code == 401:
            # token 過期或 cookie 被吃掉 → 清 cookie 重 login → retry 一次
            self._client.cookies.clear()
            self._logged_in = False
            await self._login()
            resp = await self._send(method, path, params=params, json=json)

        if resp.status_code >= 400:
            detail = _extract_detail(resp) or f"後端錯誤 {resp.status_code}"
            raise IvyApiError(resp.status_code, detail)

        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict],
        json: Optional[dict],
    ) -> httpx.Response:
        try:
            return await self._client.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise IvyApiError(
                500,
                f"後端連線失敗：{exc.__class__.__name__}",
            ) from exc


def _extract_detail(resp: httpx.Response) -> str:
    """從 FastAPI 4xx response 取 detail 字串；422 結構轉成可讀。"""
    try:
        data = resp.json()
    except ValueError:
        return resp.text.strip()[:500]
    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            # FastAPI 422 ValidationError 結構
            parts = []
            for item in detail:
                loc = ".".join(str(x) for x in item.get("loc", []))
                msg = item.get("msg", "")
                parts.append(f"{loc}: {msg}" if loc else msg)
            return "; ".join(parts) or "驗證失敗"
        if detail is not None:
            return str(detail)
    return str(data)[:500]
