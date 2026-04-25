"""
Portal package - combines all portal sub-routers.

Portal 為教師自助介面：所有路由皆掛 router-level `require_non_parent_role`，
確保家長 token 即使被誤用也撞不進員工端 endpoint（結構性 IDOR 隔離）。
"""

from fastapi import APIRouter, Depends

from utils.auth import require_non_parent_role

from .attendance import router as attendance_router
from .anomalies import router as anomalies_router
from .leaves import router as leaves_router, init_leave_notify
from .overtimes import router as overtimes_router, init_overtime_notify
from .salary import router as salary_router
from .students import router as students_router
from .calendar import router as calendar_router
from .announcements import router as announcements_router
from .profile import router as profile_router
from .schedule import router as schedule_router
from .punch_corrections import router as punch_corrections_router
from .incidents import router as incidents_router
from .assessments import router as assessments_router
from .student_attendance import router as student_attendance_router
from .dismissal_calls import router as dismissal_calls_router
from .activity import router as activity_router

router = APIRouter(
    prefix="/api/portal",
    tags=["portal"],
    dependencies=[Depends(require_non_parent_role())],
)

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
router.include_router(incidents_router, tags=["portal-incidents"])
router.include_router(assessments_router, tags=["portal-assessments"])
router.include_router(student_attendance_router, tags=["portal-student-attendance"])
router.include_router(dismissal_calls_router, tags=["portal-dismissal-calls"])
router.include_router(activity_router, tags=["portal-activity"])


def init_portal_notify_services(line_service):
    """注入 LINE 通知服務至 portal 子模組"""
    init_leave_notify(line_service)
    init_overtime_notify(line_service)
