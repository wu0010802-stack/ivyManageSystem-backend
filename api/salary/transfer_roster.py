"""
api/salary/transfer_roster.py — 銀行轉帳名冊匯出

GET /api/salaries/{year}/{month}/transfer-roster?type=base|festival|surplus|art_teacher
"""

import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from urllib.parse import quote

from models.base import session_scope
from services.finance.salary_access import enforce_full_salary_view
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
    # 全員銀行帳號 + 淨薪的跨員工匯出，僅限 admin/hr（FULL_SALARY_ROLES）。對齊
    # records.py export_all_salaries / festival.py 的 self-or-full 守衛，否則持
    # SALARY_READ 但非 admin/hr 的角色（principal/accountant）可越權匯出全員 PII。
    enforce_full_salary_view(current_user, detail="銀行轉帳名冊僅限 admin/hr 角色匯出")
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
