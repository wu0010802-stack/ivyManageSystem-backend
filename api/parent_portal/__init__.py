"""api/parent_portal/ — 家長入口子模組聚合

prefix=/api/parent（除了行政端綁定碼 endpoint 為 /api/guardians/...）。
所有 endpoint 走 role='parent' 隔離，資源存取以 user_id → Guardian
過濾（見 _shared.py）。

Batch 2 範圍：認證 + 行政發碼端點。後續 batch（3–7）會在此目錄新增
profile/attendance/announcements/leaves/fees/events/activity 子模組。
"""

from fastapi import APIRouter, Depends

from utils.auth import require_parent_role
from ._consent_gate import require_current_consent

from api.guardians_admin import (
    router as binding_admin_router,
)  # 已搬出 parent_portal/，僅保留 import 別名以維 admin_router 結構
from .auth import (
    init_line_login_service,
    router as auth_router,
)
from .activity import router as activity_router
from .announcements import router as announcements_router
from .attendance import router as attendance_router
from .calendar import router as calendar_router
from .contact_book import router as contact_book_router
from .growth_reports import router as growth_reports_router
from .measurements import router as measurements_router
from .milestones import router as milestones_router
from .photos import router as photos_router
from .timeline import router as timeline_router
from .events import router as events_router
from .family import router as family_router
from .fees import router as fees_router
from .home import router as home_router
from .leaves import router as leaves_router
from .medications import router as medications_router
from .messages import router as messages_router
from .notifications import router as notifications_router
from .parent_downloads import router as parent_downloads_router
from .profile import router as profile_router
from .assistant import router as assistant_router
from .data_export import router as data_export_router
from .consent import router as consent_router
from .dsr import router as dsr_router

# 家長端 router（前綴 /api/parent）
parent_router = APIRouter(
    prefix="/api/parent",
    tags=["parent-portal"],
)
# auth 子模組需要例外：liff-login / bind 在尚無 access token 前也得通；
# bind-additional / logout 自帶 require_parent_role dependency。
# 資料子模組（21 個）在 router 層掛 require_current_consent()，
# gate 內部已含 require_parent_role()；各端點本身仍個別掛 require_parent_role
# 以取得 current_user 注入，行為與 P2-2 前相同。
# 豁免（不掛 consent gate）：auth（登入/綁定無 token）、consent（簽署端點本身）、
# dsr 與 data_export（個資法查閱權，不可被 consent 擋）。
parent_router.include_router(auth_router)
parent_router.include_router(consent_router)
parent_router.include_router(dsr_router)
parent_router.include_router(data_export_router)

# 資料 router（21 個）：統一掛 require_current_consent() router-level gate。
_consent_dep = [Depends(require_current_consent())]
parent_router.include_router(profile_router, dependencies=_consent_dep)
parent_router.include_router(home_router, dependencies=_consent_dep)
parent_router.include_router(family_router, dependencies=_consent_dep)
parent_router.include_router(attendance_router, dependencies=_consent_dep)
parent_router.include_router(announcements_router, dependencies=_consent_dep)
parent_router.include_router(events_router, dependencies=_consent_dep)
parent_router.include_router(leaves_router, dependencies=_consent_dep)
parent_router.include_router(fees_router, dependencies=_consent_dep)
parent_router.include_router(activity_router, dependencies=_consent_dep)
parent_router.include_router(medications_router, dependencies=_consent_dep)
parent_router.include_router(messages_router, dependencies=_consent_dep)
parent_router.include_router(notifications_router, dependencies=_consent_dep)
parent_router.include_router(parent_downloads_router, dependencies=_consent_dep)
parent_router.include_router(calendar_router, dependencies=_consent_dep)
parent_router.include_router(contact_book_router, dependencies=_consent_dep)
parent_router.include_router(timeline_router, dependencies=_consent_dep)
parent_router.include_router(growth_reports_router, dependencies=_consent_dep)
parent_router.include_router(milestones_router, dependencies=_consent_dep)
parent_router.include_router(measurements_router, dependencies=_consent_dep)
parent_router.include_router(photos_router, dependencies=_consent_dep)
parent_router.include_router(assistant_router, dependencies=_consent_dep)


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
