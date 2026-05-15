"""考核報表 router。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalSummary,
)
from models.database import get_session_dep
from services.appraisal.excel_io import (
    build_cycle_report,
    build_participant_sheet,
    build_transfer_roster,
    import_cycle_excel,
)
from utils.auth import require_staff_permission
from utils.permissions import Permission

router = APIRouter()

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/cycles/{cycle_id}/report")
def cycle_report_json(
    cycle_id: int,
    db: Session = Depends(get_session_dep),
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
    db: Session = Depends(get_session_dep),
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


@router.get("/cycles/{cycle_id}/transfer_roster.xlsx")
def transfer_roster_xlsx(
    cycle_id: int,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.APPRAISAL_READ)),
):
    cycle = db.get(AppraisalCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")
    content = build_transfer_roster(db, cycle)
    return Response(
        content=content,
        media_type=XLSX_CONTENT_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="appraisal_transfer_roster_{cycle_id}.xlsx"',
        },
    )


@router.post("/cycles/{cycle_id}/import.xlsx")
async def import_cycle_xlsx(
    cycle_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(
        require_staff_permission(Permission.APPRAISAL_EVENT_WRITE)
    ),
):
    """匯入半年考核 Excel，將每行映射為 participant + score_items。

    冪等：UNIQUE(participant, item_code) 走 upsert。
    """
    cycle = db.get(AppraisalCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")
    if cycle.status.value != "OPEN":
        raise HTTPException(400, f"cycle_status_invalid:{cycle.status.value}")
    body = await file.read()
    if not body:
        raise HTTPException(400, "empty_file")
    stats = import_cycle_excel(
        db, cycle, body, actor_user_id=current_user.get("user_id")
    )
    db.commit()
    request.state.audit_entity_id = cycle_id
    request.state.audit_changes = stats
    return stats


@router.get("/participants/{participant_id}/sheet.xlsx")
def participant_sheet_xlsx(
    participant_id: int,
    db: Session = Depends(get_session_dep),
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
