"""
api/salary/snapshots.py — 薪資快照（SalarySnapshot CRUD）

含 4 個 endpoint + 1 個 schema：
- GET    /salaries/snapshots                       列表
- GET    /salaries/snapshots/{snapshot_id}         單筆詳情
- POST   /salaries/snapshots                       手動補拍
- GET    /salaries/snapshots/{snapshot_id}/diff    與當前 record 比對

所有 symbol 僅本模組內使用（HTTP 測試走 TestClient，無外部直接 import），
不需在 api.salary.__init__ re-export。
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from models.base import session_scope
from models.database import SalarySnapshot
from services import salary_snapshot_service as _snapshot_svc
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.salary_access import (
    enforce_self_or_full_salary as _enforce_self_or_full_salary,
    resolve_salary_viewer_employee_id as _resolve_salary_viewer_employee_id,
)
from schemas.salary_snapshots import (
    SalarySnapshotCreateResultOut,
    SalarySnapshotDetailOut,
    SalarySnapshotDiffOut,
    SalarySnapshotListOut,
)

router = APIRouter()


class ManualSnapshotRequest(BaseModel):
    remark: Optional[str] = Field(None, max_length=500)
    employee_id: Optional[int] = Field(None, ge=1, description="空值表示整月快照")


@router.get("/salaries/snapshots", response_model=SalarySnapshotListOut)
def list_salary_snapshots(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    employee_id: Optional[int] = Query(None, ge=1),
):
    """列出某月薪資快照（精簡 metadata）。"""
    viewer_employee_id = _resolve_salary_viewer_employee_id(current_user)
    if viewer_employee_id is not None:
        if employee_id is None:
            employee_id = viewer_employee_id
        elif employee_id != viewer_employee_id:
            raise HTTPException(status_code=403, detail="僅可查詢本人薪資")
    with session_scope() as session:
        return {
            "snapshots": _snapshot_svc.list_snapshots(session, year, month, employee_id)
        }


@router.get("/salaries/snapshots/{snapshot_id}", response_model=SalarySnapshotDetailOut)
def get_salary_snapshot(
    snapshot_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """取得單筆快照完整欄位。"""
    with session_scope() as session:
        snap_owner = (
            session.query(SalarySnapshot.employee_id)
            .filter(SalarySnapshot.id == snapshot_id)
            .scalar()
        )
        if snap_owner is None:
            raise HTTPException(status_code=404, detail="找不到該薪資快照")
        _enforce_self_or_full_salary(current_user, snap_owner)
        data = _snapshot_svc.get_snapshot_detail(session, snapshot_id)
        if data is None:
            raise HTTPException(status_code=404, detail="找不到該薪資快照")
        return data


@router.post("/salaries/snapshots", response_model=SalarySnapshotCreateResultOut)
def create_manual_salary_snapshot(
    data: ManualSnapshotRequest,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
):
    """手動補拍快照（type='manual'）。"""
    operator = current_user.get("username") or current_user.get("name") or "管理員"
    with session_scope() as session:
        count = _snapshot_svc.create_manual_snapshot(
            session,
            year=year,
            month=month,
            captured_by=operator,
            remark=data.remark,
            employee_id=data.employee_id,
        )
        if count == 0:
            raise HTTPException(
                status_code=404,
                detail=f"{year} 年 {month} 月無對應薪資記錄可建立快照",
            )
        return {
            "message": f"已建立 {count} 筆手動快照",
            "count": count,
            "captured_by": operator,
        }


@router.get("/salaries/snapshots/{snapshot_id}/diff", response_model=SalarySnapshotDiffOut)
def get_salary_snapshot_diff(
    snapshot_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """比對快照與當前 SalaryRecord 的欄位差異。"""
    with session_scope() as session:
        snap_owner = (
            session.query(SalarySnapshot.employee_id)
            .filter(SalarySnapshot.id == snapshot_id)
            .scalar()
        )
        if snap_owner is None:
            raise HTTPException(status_code=404, detail="找不到該薪資快照")
        _enforce_self_or_full_salary(current_user, snap_owner)
        data = _snapshot_svc.diff_with_current(session, snapshot_id)
        if data is None:
            raise HTTPException(status_code=404, detail="找不到該薪資快照")
        return data
