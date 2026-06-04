"""api/provenance — 自動推導值下鑽（provenance 深度3）。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from models.base import get_session_dep
from models.employee import Employee
from models.year_end import YearEndCycle
from schemas.provenance import DerivedValue
from services.provenance import resolve_provenance, KNOWN_KEYS
from utils.auth import require_permission
from utils.permissions import Permission

provenance_router = APIRouter(prefix="/api/provenance", tags=["provenance"])
logger = logging.getLogger(__name__)


@provenance_router.get("/{key}", response_model=DerivedValue)
def get_provenance(
    key: str,
    cycle_id: int = Query(...),
    employee_id: int = Query(...),
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    """回傳單一 key 的 DerivedValue（含逐筆 source_records）。"""
    cycle = session.get(YearEndCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404, "cycle 不存在")
    emp = session.get(Employee, employee_id)
    if emp is None:
        raise HTTPException(404, "employee 不存在")
    if key not in KNOWN_KEYS:
        raise HTTPException(400, f"未知的 provenance key: {key}")
    return resolve_provenance(session, cycle, emp, key)


__all__ = ["provenance_router"]
