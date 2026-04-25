"""api/parent_portal/ — 家長入口子模組聚合

prefix=/api/parent（除了行政端綁定碼 endpoint 為 /api/guardians/...）。
所有 endpoint 走 role='parent' 隔離，資源存取以 user_id → Guardian
過濾（見 _shared.py）。

Batch 2 範圍：認證 + 行政發碼端點。後續 batch（3–7）會在此目錄新增
profile/attendance/announcements/leaves/fees/events/activity 子模組。
"""

from fastapi import APIRouter, Depends

from utils.auth import require_parent_role

from .auth import (
    init_line_login_service,
    router as auth_router,
)
from .announcements import router as announcements_router
from .attendance import router as attendance_router
from .binding_admin import router as binding_admin_router
from .events import router as events_router
from .profile import router as profile_router

# 家長端 router（前綴 /api/parent，並掛 require_parent_role 統一擋線）
parent_router = APIRouter(
    prefix="/api/parent",
    tags=["parent-portal"],
)
# auth 子模組需要例外：liff-login / bind 在尚無 access token 前也得通；
# bind-additional / logout 自帶 require_parent_role dependency。
# 因此這個 parent_router 不掛 router-level dependency；其他子模組（profile
# / attendance / ...）在自身 endpoint 內掛 require_parent_role 即可，
# 並一律經 _assert_student_owned 進行 IDOR 過濾。
parent_router.include_router(auth_router)
parent_router.include_router(profile_router)
parent_router.include_router(attendance_router)
parent_router.include_router(announcements_router)
parent_router.include_router(events_router)


# 行政端綁定碼 router（前綴 /api/guardians，需 GUARDIANS_WRITE）
admin_router = APIRouter(prefix="/api", tags=["parent-bind-admin"])
admin_router.include_router(binding_admin_router)


def init_parent_line_service(line_login_service) -> None:
    """注入 LINE Login 服務（main.py 啟動時呼叫一次）。"""
    init_line_login_service(line_login_service)


__all__ = [
    "parent_router",
    "admin_router",
    "init_parent_line_service",
]
