"""
api/activity/__init__.py — 才藝系統 package 入口

彙整所有子 router；init_activity_services 已於 PR-D (2026-05-26) 退役（activity
通知改走 services.notification.dispatch.enqueue）。
"""

from fastapi import APIRouter

from ._shared import RegistrationTimeSettings  # noqa: F401（測試 import）

from .stats import router as _stats_router
from .registrations_static import router as _registrations_static_router
from .registrations_pending import router as _registrations_pending_router
from .registrations import router as _registrations_router
from .registrations_payments import router as _registrations_payments_router
from .registrations_items import router as _registrations_items_router
from .courses import router as _courses_router
from .supplies import router as _supplies_router
from .inquiries import router as _inquiries_router
from .settings import router as _settings_router
from .public import router as _public_router
from .attendance import router as _attendance_router
from .pos import router as _pos_router
from .pos_approval import router as _pos_approval_router

router = APIRouter(prefix="/api/activity", tags=["activity"])

# 順序重要：靜態路由必須優先 include；registrations_static 含 batch-payment / export /
# payment-report，必須在 _registrations_router（含 /registrations/{id}）之前 include。
# registrations_pending 含 /pending、/students/search 等字面路徑，亦須優先。
# registrations_payments / registrations_items 含 /{id}/payments、/{id}/courses、
# /{id}/supplies 等子路徑，深度與 /{id} 不衝突，安全置於 _registrations_router 之後。
router.include_router(_stats_router)
router.include_router(_pos_approval_router)
router.include_router(_pos_router)
router.include_router(_registrations_static_router)
router.include_router(_registrations_pending_router)
router.include_router(_registrations_router)
router.include_router(_registrations_payments_router)
router.include_router(_registrations_items_router)
router.include_router(_courses_router)
router.include_router(_supplies_router)
router.include_router(_inquiries_router)
router.include_router(_settings_router)
router.include_router(_public_router)
router.include_router(_attendance_router)
