"""年終獎金 Excel I/O router（import 特別獎金 / export 名冊與獎金條）。"""

from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from sqlalchemy.orm import Session

from models.database import get_session_dep
from models.year_end import (
    SpecialBonusType,
    YearEndCycle,
    YearEndSettlement,
)
from services.year_end.excel_io import (
    build_employee_slip,
    build_settlement_report,
    build_transfer_roster,
    import_special_bonus_excel,
)
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)
router = APIRouter()

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.post("/cycles/{cycle_id}/special_bonuses/import")
async def import_special_bonus(
    cycle_id: int,
    request: Request,
    bonus_type: SpecialBonusType = Form(...),
    period_label: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_WRITE)),
):
    cycle = db.get(YearEndCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")
    body = await file.read()
    if not body:
        raise HTTPException(400, "empty_file")
    stats = import_special_bonus_excel(
        db, cycle, bonus_type, body, period_label=period_label
    )
    db.commit()
    request.state.audit_entity_id = cycle_id
    request.state.audit_changes = {**stats, "bonus_type": bonus_type.value}
    return stats


@router.get("/cycles/{cycle_id}/settlement_report.xlsx")
def export_settlement_report(
    cycle_id: int,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_READ)),
):
    cycle = db.get(YearEndCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")
    content = build_settlement_report(db, cycle)
    return Response(
        content=content,
        media_type=XLSX_CONTENT_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="year_end_report_{cycle.academic_year}.xlsx"',
        },
    )


@router.get("/cycles/{cycle_id}/transfer_roster.xlsx")
def export_transfer_roster(
    cycle_id: int,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_READ)),
):
    cycle = db.get(YearEndCycle, cycle_id)
    if not cycle:
        raise HTTPException(404, "cycle_not_found")
    content = build_transfer_roster(db, cycle)
    return Response(
        content=content,
        media_type=XLSX_CONTENT_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="year_end_transfer_{cycle.academic_year}.xlsx"',
        },
    )


@router.get("/settlements/{settlement_id}/slip.xlsx")
def export_employee_slip(
    settlement_id: int,
    db: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.YEAR_END_READ)),
):
    s = db.get(YearEndSettlement, settlement_id)
    if not s:
        raise HTTPException(404, "settlement_not_found")
    content = build_employee_slip(db, s)
    return Response(
        content=content,
        media_type=XLSX_CONTENT_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="year_end_slip_{settlement_id}.xlsx"',
        },
    )
