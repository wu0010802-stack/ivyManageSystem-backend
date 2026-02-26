"""
Attendance package - combines all attendance sub-routers.
"""

from fastapi import APIRouter

from .upload import router as upload_router
from .records import router as records_router
from .reports import router as reports_router

router = APIRouter(prefix="/api", tags=["attendance"])

router.include_router(upload_router, prefix="/attendance")
router.include_router(records_router, prefix="/attendance")
router.include_router(reports_router, prefix="/attendance")
