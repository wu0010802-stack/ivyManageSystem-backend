"""
Portal package - combines all portal sub-routers.

Portal 為教師自助介面：所有路由皆掛 router-level `require_non_parent_role`，
確保家長 token 即使被誤用也撞不進員工端 endpoint（結構性 IDOR 隔離）。
"""

from fastapi import APIRouter, Depends

from utils.auth import require_non_parent_role

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
from .incidents import router as incidents_router
from .assessments import router as assessments_router
from .student_attendance import router as student_attendance_router
from .dismissal_calls import router as dismissal_calls_router
from .activity import router as activity_router
from .contact_book import (
    init_contact_book_line_service,
    router as contact_book_router,
)
from .contact_book_templates import router as contact_book_templates_router
from .home import router as home_router
from .medications import router as medications_router
from .class_hub import router as class_hub_router
from .parent_messages import router as parent_messages_router
from .search import router as search_router
from .appraisal import router as appraisal_router
from .leaves_quota_expiry import router as leaves_quota_expiry_router
from .comp_leave_history import router as comp_leave_history_router
from .data_export import router as data_export_router
from .punch_pin import router as punch_pin_router

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
router.include_router(parent_messages_router, tags=["portal-parent-messages"])
router.include_router(contact_book_router, tags=["portal-contact-book"])
router.include_router(
    contact_book_templates_router, tags=["portal-contact-book-templates"]
)
router.include_router(home_router, tags=["portal-home"])
router.include_router(medications_router, tags=["portal-medications"])
router.include_router(class_hub_router, tags=["portal-class-hub"])
router.include_router(search_router)
router.include_router(appraisal_router, tags=["portal-appraisal"])
router.include_router(leaves_quota_expiry_router, tags=["portal-leave-quota-expiry"])
router.include_router(comp_leave_history_router, tags=["portal-comp-leave-history"])
router.include_router(data_export_router, tags=["portal-data-export"])
router.include_router(punch_pin_router, tags=["portal-punch-pin"])


def init_portal_notify_services(line_service):
    """PR-D (2026-05-26) 之後僅保留 contact_book LINE service injection（
    test fixture 仍透過 init_contact_book_line_service 設 mock 服務）；leaves /
    overtimes / parent_messages 已改走 services.notification.dispatch.enqueue，
    無需 service 注入。"""
    init_contact_book_line_service(line_service)
