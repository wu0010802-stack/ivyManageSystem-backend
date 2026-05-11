"""考核報表 router。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalSummary,
)
from models.database import get_session
from services.appraisal_excel import (
    build_cycle_report,
    build_participant_sheet,
    build_penalty_log,
)
from utils.auth import require_staff_permission
from utils.permissions import Permission

router = APIRouter()

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/cycles/{cycle_id}/report")
def cycle_report_json(
    cycle_id: int,
    db: Session = Depends(get_session),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    cycle = db.get(AppraisalCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")
    summaries = (
        db.execute(
            select(AppraisalSummary).where(AppraisalSummary.cycle_id == cycle_id)
        )
        .scalars()
        .all()
    )
    return {
        "cycle": {
            "id": cycle.id,
            "academic_year": cycle.academic_year,
            "semester": cycle.semester.value,
            "status": cycle.status.value,
        },
        "summaries": [
            {
                "id": s.id,
                "participant_id": s.participant_id,
                "base_score": str(s.base_score),
                "total_score": str(s.total_score),
                "grade": s.grade.value,
                "bonus_amount": str(s.bonus_amount),
                "status": s.status.value,
            }
            for s in summaries
        ],
    }


@router.get("/cycles/{cycle_id}/report.xlsx")
def cycle_report_xlsx(
    cycle_id: int,
    db: Session = Depends(get_session),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    cycle = db.get(AppraisalCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")
    content = build_cycle_report(db, cycle)
    return Response(
        content=content,
        media_type=XLSX_CONTENT_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="appraisal_cycle_{cycle_id}.xlsx"',
        },
    )


@router.get("/cycles/{cycle_id}/penalty_log.xlsx")
def penalty_log_xlsx(
    cycle_id: int,
    db: Session = Depends(get_session),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    cycle = db.get(AppraisalCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")
    content = build_penalty_log(db, cycle)
    return Response(
        content=content,
        media_type=XLSX_CONTENT_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="appraisal_penalty_log_{cycle_id}.xlsx"',
        },
    )


@router.get("/participants/{participant_id}/sheet.xlsx")
def participant_sheet_xlsx(
    participant_id: int,
    db: Session = Depends(get_session),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    p = db.get(AppraisalParticipant, participant_id)
    if not p:
        raise HTTPException(404, "participant_not_found")
    content = build_participant_sheet(db, p)
    return Response(
        content=content,
        media_type=XLSX_CONTENT_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="appraisal_sheet_{participant_id}.xlsx"',
        },
    )
