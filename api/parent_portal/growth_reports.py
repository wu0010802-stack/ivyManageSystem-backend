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
from sqlalchemy import func

from api.portfolio.reports import _resolve_pdf_path
from models.database import StudentGrowthReport, get_session
from models.portfolio import REPORT_STATUS_READY
from utils.auth import require_parent_role
from utils.errors import raise_safe_500

from ._shared import _assert_student_owned

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/growth-reports", tags=["parent-growth-reports"])


def _parent_row_to_dict(r: StudentGrowthReport) -> dict:
    """F-V6-06：家長端報告序列化白名單。

    刻意不重用 api/portfolio/reports.py 的 _row_to_dict —— 後者含 error_message
    / file_path / generated_by 等 admin 內部欄位；admin 把失敗 report 補 patch
    後改回 status=READY 時 error_message 殘留會洩漏內部錯誤訊息給家長。
    """
    return {
        "id": r.id,
        "student_id": r.student_id,
        "period_label": r.period_label,
        "period_start": r.period_start.isoformat() if r.period_start else None,
        "period_end": r.period_end.isoformat() if r.period_end else None,
        "status": r.status,
        "file_size": r.file_size,
        "generated_at": r.generated_at.isoformat() if r.generated_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "line_sent_at": r.line_sent_at.isoformat() if r.line_sent_at else None,
        "parent_first_viewed_at": (
            r.parent_first_viewed_at.isoformat() if r.parent_first_viewed_at else None
        ),
        "parent_view_count": r.parent_view_count,
        "teacher_narrative": r.teacher_narrative,
    }


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
            return {"items": [_parent_row_to_dict(r) for r in rows]}
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
            # 原子化 INCR + COALESCE：避免並發雙擊時 read-modify-write 的 lost
            # update（agent P2 #10，同 dismissal 2026-05-12 round 3 修補 idiom）。
            # UPDATE ... SET col = col + 1 在 row 層 exclusive lock，無 race。
            now = datetime.utcnow()
            session.query(StudentGrowthReport).filter_by(
                id=report_id, student_id=student_id
            ).update(
                {
                    "parent_view_count": (
                        func.coalesce(StudentGrowthReport.parent_view_count, 0) + 1
                    ),
                    "parent_first_viewed_at": func.coalesce(
                        StudentGrowthReport.parent_first_viewed_at, now
                    ),
                },
                synchronize_session=False,
            )
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
