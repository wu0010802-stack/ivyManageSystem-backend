"""
api/salary/transfer_roster.py — 銀行轉帳名冊匯出

GET /api/salaries/{year}/{month}/transfer-roster?type=base|festival|surplus|art_teacher
"""

import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from urllib.parse import quote

from models.base import session_scope
from services.transfer_roster import ROSTER_TYPES, generate_transfer_roster
from utils.auth import require_staff_permission
from utils.permissions import Permission

router = APIRouter()


@router.get("/salaries/{year}/{month}/transfer-roster")
def export_transfer_roster(
    year: int,
    month: int,
    type: str = Query(..., description="名冊類型：base|festival|surplus|art_teacher"),
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """匯出指定月份的銀行轉帳名冊 xlsx。"""
    if type not in ROSTER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的名冊類型，須為 {', '.join(ROSTER_TYPES)}",
        )
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="month 須介於 1~12")

    with session_scope() as session:
        filename, xlsx_bytes = generate_transfer_roster(session, year, month, type)

    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (f"attachment; filename*=UTF-8''{quote(filename)}")
        },
    )
