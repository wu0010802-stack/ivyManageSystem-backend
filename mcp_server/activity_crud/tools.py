"""tools.py — 註冊 12 個 MCP tool（課程 8 + 用品 4）。

每個 tool 對應一個 ivy-backend REST endpoint：
- 收到 LLM 的 primitive args → 組 request body → 交給 IvyApiClient
- 後端 4xx/5xx → IvyApiError 由 register_tools 包成 MCP error 訊息

故意不重用後端 schema，因為：
- 後端 schema 含 time 型別 → MCP tool JSON schema 對 LLM 不友善（time 改 "HH:MM" str）
- 引入 schemas.activity_admin 會連帶把後端 import surface 拉進來
- 後端會用自己的 schema 再驗一次，雙重驗證不冗
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .client import IvyApiClient, IvyApiError


def register_tools(mcp: FastMCP, client: IvyApiClient) -> None:
    """把 12 個 tool 註冊到 mcp instance。

    呼叫一次，把所有 @mcp.tool 綁定到傳入的 mcp、共用同一個 client（cookie jar）。
    """

    # ── 課程（8）─────────────────────────────────────────────────────────

    @mcp.tool()
    async def list_courses(
        school_year: Optional[int] = None,
        semester: Optional[int] = None,
        skip: int = 0,
        limit: int = 200,
    ) -> dict:
        """列出課程（含已報名 / 候補 / 剩餘容量統計）。

        Args:
            school_year: 民國學年（例 114）；不填則用後端當前學年
            semester: 學期 1 或 2；不填則用後端當前學期
            skip: 分頁偏移，預設 0
            limit: 每頁筆數，預設 200，最大 500

        Returns:
            { courses: [...], total, skip, limit, school_year, semester }
        """
        return await _call(
            client,
            "GET",
            "/api/activity/courses",
            params=_compact(
                {
                    "school_year": school_year,
                    "semester": semester,
                    "skip": skip,
                    "limit": limit,
                }
            ),
        )

    @mcp.tool()
    async def get_course(course_id: int) -> dict:
        """取得單一課程詳情。"""
        return await _call(client, "GET", f"/api/activity/courses/{course_id}")

    @mcp.tool()
    async def create_course(
        name: str,
        price: int,
        capacity: int = 30,
        sessions: Optional[int] = None,
        video_url: Optional[str] = None,
        allow_waitlist: bool = True,
        description: Optional[str] = None,
        school_year: Optional[int] = None,
        semester: Optional[int] = None,
        min_age_months: Optional[int] = None,
        max_age_months: Optional[int] = None,
        meeting_weekday: Optional[int] = None,
        meeting_start_time: Optional[str] = None,
        meeting_end_time: Optional[str] = None,
    ) -> dict:
        """新增課程（同學期內名稱唯一；單價超過閾值需 ACTIVITY_APPROVE）。

        Args:
            name: 課程名稱（1-100 字）
            price: 單價（元，0 ~ 9,999,999）
            capacity: 名額上限（>=1，預設 30）
            sessions: 堂數（>=1，可空）
            video_url: 介紹影片連結（可空）
            allow_waitlist: 是否開放候補（預設 True）
            description: 簡介（可空）
            school_year: 民國學年（不填用當前學年）
            semester: 學期（不填用當前學期）
            min_age_months: 適齡下限（月，0-360）
            max_age_months: 適齡上限（月，0-360）
            meeting_weekday: 上課星期（0=週一 ~ 6=週日）
            meeting_start_time: 上課開始時間 "HH:MM"
            meeting_end_time: 上課結束時間 "HH:MM"
        """
        body = _compact(
            {
                "name": name,
                "price": price,
                "capacity": capacity,
                "sessions": sessions,
                "video_url": video_url,
                "allow_waitlist": allow_waitlist,
                "description": description,
                "school_year": school_year,
                "semester": semester,
                "min_age_months": min_age_months,
                "max_age_months": max_age_months,
                "meeting_weekday": meeting_weekday,
                "meeting_start_time": meeting_start_time,
                "meeting_end_time": meeting_end_time,
            }
        )
        return await _call(client, "POST", "/api/activity/courses", json=body)

    @mcp.tool()
    async def copy_courses_from_previous(
        source_school_year: int,
        source_semester: int,
        target_school_year: int,
        target_semester: int,
    ) -> dict:
        """從來源學期一鍵複製所有課程到目標學期。

        - 來源學期內所有 is_active 課程都會被複製
        - 已存在同名課程不會重複建立（跳過）
        """
        body = {
            "source_school_year": source_school_year,
            "source_semester": source_semester,
            "target_school_year": target_school_year,
            "target_semester": target_semester,
        }
        return await _call(
            client,
            "POST",
            "/api/activity/courses/copy-from-previous",
            json=body,
        )

    @mcp.tool()
    async def update_course(
        course_id: int,
        name: Optional[str] = None,
        price: Optional[int] = None,
        sessions: Optional[int] = None,
        capacity: Optional[int] = None,
        video_url: Optional[str] = None,
        allow_waitlist: Optional[bool] = None,
        description: Optional[str] = None,
        min_age_months: Optional[int] = None,
        max_age_months: Optional[int] = None,
        meeting_weekday: Optional[int] = None,
        meeting_start_time: Optional[str] = None,
        meeting_end_time: Optional[str] = None,
    ) -> dict:
        """更新課程（只送有提供的欄位；學期欄位不可改）。"""
        body = _compact(
            {
                "name": name,
                "price": price,
                "sessions": sessions,
                "capacity": capacity,
                "video_url": video_url,
                "allow_waitlist": allow_waitlist,
                "description": description,
                "min_age_months": min_age_months,
                "max_age_months": max_age_months,
                "meeting_weekday": meeting_weekday,
                "meeting_start_time": meeting_start_time,
                "meeting_end_time": meeting_end_time,
            }
        )
        return await _call(
            client, "PUT", f"/api/activity/courses/{course_id}", json=body
        )

    @mcp.tool()
    async def delete_course(course_id: int) -> dict:
        """停用課程（軟刪 is_active=False；不影響歷史報名）。"""
        return await _call(client, "DELETE", f"/api/activity/courses/{course_id}")

    @mcp.tool()
    async def list_course_waitlist(course_id: int) -> dict:
        """列出課程候補名單（依候補時間排序）。"""
        return await _call(client, "GET", f"/api/activity/courses/{course_id}/waitlist")

    @mcp.tool()
    async def list_course_enrolled(course_id: int) -> dict:
        """列出課程已報名學生（含繳費狀態）。"""
        return await _call(client, "GET", f"/api/activity/courses/{course_id}/enrolled")

    # ── 用品（4）─────────────────────────────────────────────────────────

    @mcp.tool()
    async def list_supplies(
        school_year: Optional[int] = None,
        semester: Optional[int] = None,
        skip: int = 0,
        limit: int = 200,
    ) -> dict:
        """列出用品（依學期過濾，支援分頁）。"""
        return await _call(
            client,
            "GET",
            "/api/activity/supplies",
            params=_compact(
                {
                    "school_year": school_year,
                    "semester": semester,
                    "skip": skip,
                    "limit": limit,
                }
            ),
        )

    @mcp.tool()
    async def create_supply(
        name: str,
        price: int,
        school_year: Optional[int] = None,
        semester: Optional[int] = None,
    ) -> dict:
        """新增用品（同學期內名稱唯一；高單價需簽核）。"""
        body = _compact(
            {
                "name": name,
                "price": price,
                "school_year": school_year,
                "semester": semester,
            }
        )
        return await _call(client, "POST", "/api/activity/supplies", json=body)

    @mcp.tool()
    async def update_supply(
        supply_id: int,
        name: Optional[str] = None,
        price: Optional[int] = None,
    ) -> dict:
        """更新用品（只送有提供的欄位）。"""
        body = _compact({"name": name, "price": price})
        return await _call(
            client, "PUT", f"/api/activity/supplies/{supply_id}", json=body
        )

    @mcp.tool()
    async def delete_supply(supply_id: int) -> dict:
        """停用用品（軟刪）。"""
        return await _call(client, "DELETE", f"/api/activity/supplies/{supply_id}")


async def _call(
    client: IvyApiClient,
    method: str,
    path: str,
    *,
    params: Optional[dict] = None,
    json: Optional[dict] = None,
) -> Any:
    """共用呼叫包裝：把 IvyApiError 轉成 LLM 友善的訊息回給 MCP runtime。

    FastMCP 對 tool 拋出的 Exception 會自動轉成 isError + str(exc) 給 client。
    我們重新 raise 一個訊息中性的 Exception，避免 traceback 干擾 LLM 解讀。
    """
    try:
        return await client.request(method, path, params=params, json=json)
    except IvyApiError as exc:
        # 後端的中文 detail 直接回給 LLM；status 附在尾巴方便除錯
        raise RuntimeError(f"{exc.message}（HTTP {exc.status}）") from None


def _compact(d: dict) -> dict:
    """過濾掉 value=None 的 key，避免發送 `field=null` 造成後端誤判。"""
    return {k: v for k, v in d.items() if v is not None}
