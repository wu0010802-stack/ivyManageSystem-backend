"""懲處記錄 CRUD API。

警告/小過/大過會於下一個獎金發放期自動從節慶+超額獎金扣減。
已抵扣的懲處不可改金額（避免影響歷史薪資），可改 reason；刪除須 SALARY_WRITE 權限（hr/accountant/supervisor 等，非僅 admin），且已抵扣者不可刪。
"""

from datetime import date as _date
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import joinedload

from models.base import session_scope
from models.database import DisciplinaryAction, Employee
from models.disciplinary import (
    ACTION_TYPE_LABELS,
    ACTION_TYPES,
    MERIT_ACTION_TYPES,
)
from utils.auth import require_staff_permission
from utils.permissions import Permission

router = APIRouter(prefix="/api", tags=["disciplinary"])


# ── Pydantic schemas ────────────────────────────────────────────────────────


class DisciplinaryActionCreate(BaseModel):
    employee_id: int
    action_date: _date
    action_type: str = Field(
        ...,
        description="warning|minor|major（懲處） | commendation|minor_merit|major_merit（merit 獎勵，不扣款）",
    )
    deduction_amount: float = Field(
        0, ge=0, description="0 表示用 BonusConfig 預設；merit 類型請填 0"
    )
    reason: Optional[str] = None


class DisciplinaryActionUpdate(BaseModel):
    action_date: Optional[_date] = None
    action_type: Optional[str] = None
    deduction_amount: Optional[float] = Field(None, ge=0)
    reason: Optional[str] = None


class DisciplinaryActionOut(BaseModel):
    id: int
    employee_id: int
    employee_name: Optional[str] = None
    action_date: _date
    action_type: str
    action_type_label: str
    deduction_amount: float
    reason: Optional[str] = None
    applied_to_salary_id: Optional[int] = None
    applied_at: Optional[datetime] = None
    applied_amount: Optional[float] = None
    created_at: datetime
    created_by: Optional[str] = None


def _to_out(action: DisciplinaryAction) -> DisciplinaryActionOut:
    return DisciplinaryActionOut(
        id=action.id,
        employee_id=action.employee_id,
        employee_name=getattr(action.employee, "name", None),
        action_date=action.action_date,
        action_type=action.action_type,
        action_type_label=ACTION_TYPE_LABELS.get(
            action.action_type, action.action_type
        ),
        deduction_amount=float(action.deduction_amount or 0),
        reason=action.reason,
        applied_to_salary_id=action.applied_to_salary_id,
        applied_at=action.applied_at,
        applied_amount=(
            float(action.applied_amount) if action.applied_amount is not None else None
        ),
        created_at=action.created_at,
        created_by=action.created_by,
    )


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("/disciplinary-actions")
def list_actions(
    employee_id: Optional[int] = Query(None),
    start_date: Optional[_date] = Query(None),
    end_date: Optional[_date] = Query(None),
    pending_only: bool = Query(False, description="僅顯示未抵扣"),
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """列出懲處記錄（可篩 employee_id / 日期區間 / 僅未抵扣）。"""
    with session_scope() as session:
        q = session.query(DisciplinaryAction).options(
            joinedload(DisciplinaryAction.employee)
        )
        if employee_id is not None:
            q = q.filter(DisciplinaryAction.employee_id == employee_id)
        if start_date:
            q = q.filter(DisciplinaryAction.action_date >= start_date)
        if end_date:
            q = q.filter(DisciplinaryAction.action_date <= end_date)
        if pending_only:
            q = q.filter(DisciplinaryAction.applied_to_salary_id.is_(None))

        rows = q.order_by(
            DisciplinaryAction.action_date.desc(), DisciplinaryAction.id.desc()
        ).all()
        return {"items": [_to_out(a).model_dump() for a in rows]}


@router.post("/disciplinary-actions")
def create_action(
    payload: DisciplinaryActionCreate,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """新增懲處記錄。下一次該員工計算發放月薪資時會自動扣減節慶+超額。"""
    if payload.action_type not in ACTION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"action_type 須為 {', '.join(ACTION_TYPES)}",
        )
    if payload.action_type in MERIT_ACTION_TYPES and payload.deduction_amount > 0:
        raise HTTPException(
            status_code=422,
            detail="獎勵類型（嘉獎/小功/大功）不可填扣款金額",
        )

    with session_scope() as session:
        emp = session.query(Employee).get(payload.employee_id)
        if not emp:
            raise HTTPException(status_code=404, detail="員工不存在")

        action = DisciplinaryAction(
            employee_id=payload.employee_id,
            action_date=payload.action_date,
            action_type=payload.action_type,
            deduction_amount=payload.deduction_amount or 0,
            reason=payload.reason,
            created_by=current_user.get("username"),
            updated_by=current_user.get("username"),
        )
        session.add(action)
        session.flush()
        action.employee = emp  # 確保序列化拿得到名字
        result = _to_out(action).model_dump()
        session.commit()
        return result


@router.put("/disciplinary-actions/{action_id}")
def update_action(
    action_id: int,
    payload: DisciplinaryActionUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """編輯懲處。已抵扣的不可改金額/日期/類型，僅可改 reason。"""
    with session_scope() as session:
        action = (
            session.query(DisciplinaryAction)
            .options(joinedload(DisciplinaryAction.employee))
            .filter(DisciplinaryAction.id == action_id)
            .first()
        )
        if not action:
            raise HTTPException(status_code=404, detail="懲處記錄不存在")

        is_applied = action.applied_to_salary_id is not None

        if payload.reason is not None:
            action.reason = payload.reason

        if any(
            v is not None
            for v in (
                payload.action_date,
                payload.action_type,
                payload.deduction_amount,
            )
        ):
            if is_applied:
                raise HTTPException(
                    status_code=409,
                    detail="已抵扣懲處不可修改金額/日期/類型，僅可修改原因。",
                )
            if payload.action_type is not None:
                if payload.action_type not in ACTION_TYPES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"action_type 須為 {', '.join(ACTION_TYPES)}",
                    )
                action.action_type = payload.action_type
                # 轉成 merit 類型且本次未帶金額時，歸零殘留扣款——
                # 否則留下「獎勵 + 扣款金額」的不一致列（_effective_amount
                # 對 merit 恆回 0 守住金流，此處修資料層殘留）。
                if (
                    payload.action_type in MERIT_ACTION_TYPES
                    and payload.deduction_amount is None
                ):
                    action.deduction_amount = 0
            if payload.action_date is not None:
                action.action_date = payload.action_date
            if payload.deduction_amount is not None:
                # 更新後的 action_type（可能已在本次更新中改變）
                effective_type = action.action_type
                if (
                    effective_type in MERIT_ACTION_TYPES
                    and payload.deduction_amount > 0
                ):
                    raise HTTPException(
                        status_code=422,
                        detail="獎勵類型（嘉獎/小功/大功）不可填扣款金額",
                    )
                action.deduction_amount = payload.deduction_amount

        action.updated_by = current_user.get("username")
        session.flush()
        result = _to_out(action).model_dump()
        session.commit()
        return result


@router.delete("/disciplinary-actions/{action_id}")
def delete_action(
    action_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """刪除懲處。已抵扣的不可刪除（避免歷史薪資不一致）。"""
    with session_scope() as session:
        action = (
            session.query(DisciplinaryAction)
            .filter(DisciplinaryAction.id == action_id)
            .first()
        )
        if not action:
            raise HTTPException(status_code=404, detail="懲處記錄不存在")
        if action.applied_to_salary_id is not None:
            raise HTTPException(
                status_code=409,
                detail="已抵扣的懲處不可刪除；如需撤銷請聯絡管理員手動處理。",
            )
        session.delete(action)
        session.commit()
        return {"deleted": True, "id": action_id}
