"""api/parent_portal/family.py — 家校樞紐頁 timeline 彙整端點。

把出勤 / 公告 / 聯絡簿 / 事件簽閱 / 用藥單 / 請假審核結果 6 種資料
合成單一時間軸，避免 /family 樞紐頁需打 6 支 API。

Perf：30s in-process TTLCache（key=(user_id, student_id, limit)）；
家長端 useCachedAsync 再加 60s 前端 cache，雙層緩衝。
"""

from datetime import date, datetime, timedelta
from typing import Any

from cachetools import TTLCache
from fastapi import APIRouter, Depends, HTTPException, Query

from models.classroom import StudentAttendance
from models.database import (
    Announcement,
    AnnouncementParentRecipient,
    AnnouncementParentRead,
    EventAcknowledgment,
    Guardian,
    SchoolEvent,
    Student,
    StudentContactBookEntry,
    get_session,
)
from models.portfolio import StudentMedicationOrder
from models.student_leave import StudentLeaveRequest
from utils.auth import require_parent_role

from ._shared import _assert_student_owned

router = APIRouter(prefix="/family", tags=["parent-family"])

# (user_id, student_id, limit) → timeline payload；30s TTL
_timeline_cache: TTLCache = TTLCache(maxsize=512, ttl=30)


@router.get("/timeline")
def family_timeline(
    student_id: int = Query(..., ge=1),
    limit: int = Query(7, ge=1, le=50),
    current_user: dict = Depends(require_parent_role()),
):
    """單一子女最近 N 筆混合 timeline。

    回傳格式：
        [
            {
                "kind": "attendance" | "announcement" | "contact_book" |
                        "event_ack" | "medication" | "leave_review",
                "id": str,  # 形如 "attendance:1"，client 不解析，僅作 key
                "title": str,
                "subtitle": str | None,
                "occurred_at": str,  # ISO 8601
                "is_pending": bool,  # 待辦標紅點
                "href": str,  # 前端對應 route
            }, ...
        ]
    """
    user_id = current_user["user_id"]
    session = get_session()
    try:
        _assert_student_owned(session, user_id, student_id, for_write=False)

        cache_key = (user_id, student_id, limit)
        cached = _timeline_cache.get(cache_key)
        if cached is not None:
            return cached

        items = _collect_timeline_items(session, user_id, student_id, limit)
        _timeline_cache[cache_key] = items
        return items
    finally:
        session.close()


def _collect_timeline_items(
    session, user_id: int, student_id: int, limit: int
) -> list[dict[str, Any]]:
    """彙整 6 種來源的最新事件，按 occurred_at desc 排序、limit 切片。

    Task 2A.3 完整實作；先回空 list 讓 Task 2A.2 測試骨架可跑（403 / empty 兩條會 PASS，
    其他需要實際資料的會 FAIL）。
    """
    return []
