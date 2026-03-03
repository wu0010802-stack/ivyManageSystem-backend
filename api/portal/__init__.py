"""
Portal package - combines all portal sub-routers.
"""

from fastapi import APIRouter

from .attendance import router as attendance_router
from .anomalies import router as anomalies_router
from .leaves import router as leaves_router
from .overtimes import router as overtimes_router
from .salary import router as salary_router
from .students import router as students_router
from .calendar import router as calendar_router
from .announcements import router as announcements_router
from .profile import router as profile_router
from .schedule import router as schedule_router
from .punch_corrections import router as punch_corrections_router

router = APIRouter(prefix="/api/portal", tags=["portal"])

router.include_router(attendance_router)
router.include_router(anomalies_router)
router.include_router(leaves_router)
router.include_router(overtimes_router)
router.include_router(salary_router)
router.include_router(students_router)
router.include_router(calendar_router)
router.include_router(announcements_router)
router.include_router(profile_router)
router.include_router(schedule_router)
router.include_router(punch_corrections_router, tags=["portal-punch-corrections"])
