"""api/parent_portal/growth_reports.py — 家長端成長報告 read-only.

Endpoints:
- GET /api/parent/growth-reports?student_id=
- GET /api/parent/growth-reports/{report_id}/download?student_id=
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from api.portfolio.reports import _row_to_dict, _resolve_pdf_path
from models.database import StudentGrowthReport, get_session
from models.portfolio import REPORT_STATUS_READY
from utils.auth import require_parent_role
from utils.errors import raise_safe_500

from ._shared import _assert_student_owned

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/growth-reports", tags=["parent-growth-reports"])


@router.get("")
async def parent_list_reports(
    student_id: int = Query(...),
    current_user: dict = Depends(require_parent_role()),
) -> dict:
    try:
        session = get_session()
        try:
            user_id = current_user["user_id"]
            _assert_student_owned(session, user_id, student_id)
            rows = (
                session.query(StudentGrowthReport)
                .filter(
                    StudentGrowthReport.student_id == student_id,
                    StudentGrowthReport.status == REPORT_STATUS_READY,
                )
                .order_by(StudentGrowthReport.created_at.desc())
                .all()
            )
            return {"items": [_row_to_dict(r) for r in rows]}
        finally:
            session.close()
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="家長端查詢報告列表失敗")


@router.get("/{report_id}/download")
async def parent_download_report(
    report_id: int,
    student_id: int = Query(...),
    current_user: dict = Depends(require_parent_role()),
) -> FileResponse:
    try:
        session = get_session()
        try:
            user_id = current_user["user_id"]
            _assert_student_owned(session, user_id, student_id)
            r = (
                session.query(StudentGrowthReport)
                .filter_by(id=report_id, student_id=student_id)
                .first()
            )
            if not r:
                raise HTTPException(status_code=404, detail="報告不存在")
            if r.status != REPORT_STATUS_READY or not r.file_path:
                raise HTTPException(status_code=409, detail="報告尚未準備好")
            path = _resolve_pdf_path(r.file_path)
            if not path.exists():
                raise HTTPException(status_code=410, detail="報告檔案已遺失")
            if r.parent_first_viewed_at is None:
                r.parent_first_viewed_at = datetime.utcnow()
            r.parent_view_count = (r.parent_view_count or 0) + 1
            session.commit()
            return FileResponse(
                str(path),
                media_type="application/pdf",
                filename=f"growth_report_{r.id}.pdf",
            )
        finally:
            session.close()
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="家長端下載報告失敗")
