"""
api/activity/__init__.py — 才藝系統 package 入口

彙整所有子 router，對外保持與原 api/activity.py 完全相同的 import 介面：
    from api.activity import router as activity_router, init_activity_services
"""

from fastapi import APIRouter

from ._shared import (
    init_activity_services,
    RegistrationTimeSettings,
)  # noqa: F401（測試 import）

from .stats import router as _stats_router
from .registrations import router as _registrations_router
from .courses import router as _courses_router
from .supplies import router as _supplies_router
from .inquiries import router as _inquiries_router
from .settings import router as _settings_router
from .public import router as _public_router
from .attendance import router as _attendance_router
from .pos import router as _pos_router

router = APIRouter(prefix="/api/activity", tags=["activity"])

# 順序重要：靜態路由（batch-payment、export、pos/*）已在各自檔案內優先定義
router.include_router(_stats_router)
router.include_router(_pos_router)
router.include_router(_registrations_router)
router.include_router(_courses_router)
router.include_router(_supplies_router)
router.include_router(_inquiries_router)
router.include_router(_settings_router)
router.include_router(_public_router)
router.include_router(_attendance_router)
