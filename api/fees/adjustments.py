"""api/fees/adjustments.py — 學費折抵 CRUD（同胞優惠 / 預繳 / 請假扣款 / 其他）。

折抵類獨立於 StudentFeeRecord（正金額應收）；套用方式：
    total_due_adjusted = SUM(records.amount_due) - SUM(adjustments.amount)

不影響 amount_paid / payments 流水；折抵刪除即還原。
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from models.base import session_scope
from models.fees import StudentFeeAdjustment
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.portfolio_access import assert_student_access, is_unrestricted

logger = logging.getLogger(__name__)

router = APIRouter()

ALLOWED_TYPES = {"sibling_discount", "prepayment", "leave_deduction", "other"}
PERIOD_PATTERN = r"^\d{3}-[12]$"


class AdjustmentCreate(BaseModel):
    student_id: int = Field(..., gt=0)
    period: str = Field(..., pattern=PERIOD_PATTERN)
    adjustment_type: str = Field(...)
    amount: int = Field(..., ge=1, le=999999)
    reason: Optional[str] = Field(None, max_length=200)
    notes: Optional[str] = Field("", max_length=500)

    @field_validator("adjustment_type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        if v not in ALLOWED_TYPES:
            raise ValueError(f"非法 adjustment_type: {v}")
        return v


class AdjustmentUpdate(BaseModel):
    adjustment_type: Optional[str] = None
    amount: Optional[int] = Field(None, ge=1, le=999999)
    reason: Optional[str] = Field(None, max_length=200)
    notes: Optional[str] = Field(None, max_length=500)

    @field_validator("adjustment_type")
    @classmethod
    def _validate_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in ALLOWED_TYPES:
            raise ValueError(f"非法 adjustment_type: {v}")
        return v


def _serialize(a: StudentFeeAdjustment) -> dict:
    return {
        "id": a.id,
        "student_id": a.student_id,
        "period": a.period,
        "adjustment_type": a.adjustment_type,
        "amount": a.amount,
        "reason": a.reason,
        "notes": a.notes,
        "created_by": a.created_by,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


@router.get("/adjustments")
def list_adjustments(
    period: Optional[str] = Query(None, pattern=PERIOD_PATTERN),
    student_id: Optional[int] = Query(None, gt=0),
    adjustment_type: Optional[str] = Query(None),
    current_user: dict = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """列出學費折抵。

    Scope：非管理角色須帶 student_id 並通過 assert_student_access；
    不帶 student_id 全校列出僅限 admin/hr/supervisor。
    """
    if adjustment_type and adjustment_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail=f"非法 adjustment_type: {adjustment_type}")

    with session_scope() as session:
        if not is_unrestricted(current_user):
            if student_id is None:
                raise HTTPException(
                    status_code=403,
                    detail="非管理角色不得列出全校折抵紀錄，請指定 student_id",
                )
            assert_student_access(session, current_user, student_id)

        q = session.query(StudentFeeAdjustment)
        if period:
            q = q.filter(StudentFeeAdjustment.period == period)
        if student_id:
            q = q.filter(StudentFeeAdjustment.student_id == student_id)
        if adjustment_type:
            q = q.filter(StudentFeeAdjustment.adjustment_type == adjustment_type)
        items = (
            q.order_by(
                StudentFeeAdjustment.period.desc(),
                StudentFeeAdjustment.student_id,
                StudentFeeAdjustment.id,
            ).all()
        )
        return {"items": [_serialize(a) for a in items], "total": len(items)}


@router.post("/adjustments")
def create_adjustment(
    payload: AdjustmentCreate,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    with session_scope() as session:
        if not is_unrestricted(current_user):
            assert_student_access(session, current_user, payload.student_id)
        adj = StudentFeeAdjustment(
            student_id=payload.student_id,
            period=payload.period,
            adjustment_type=payload.adjustment_type,
            amount=payload.amount,
            reason=payload.reason,
            notes=payload.notes or "",
            created_by=current_user.get("username"),
            created_at=datetime.now(),  # noqa: DTZ005
            updated_at=datetime.now(),  # noqa: DTZ005
        )
        session.add(adj)
        session.flush()
        logger.info(
            "fee adjustment created id=%s student_id=%s period=%s type=%s amount=%s",
            adj.id, adj.student_id, adj.period, adj.adjustment_type, adj.amount,
        )
        return _serialize(adj)


@router.put("/adjustments/{adjustment_id}")
def update_adjustment(
    adjustment_id: int,
    payload: AdjustmentUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    with session_scope() as session:
        adj = session.get(StudentFeeAdjustment, adjustment_id)
        if not adj:
            raise HTTPException(status_code=404, detail="折抵紀錄不存在")
        if not is_unrestricted(current_user):
            assert_student_access(session, current_user, adj.student_id)

        if payload.adjustment_type is not None:
            adj.adjustment_type = payload.adjustment_type
        if payload.amount is not None:
            adj.amount = payload.amount
        if payload.reason is not None:
            adj.reason = payload.reason
        if payload.notes is not None:
            adj.notes = payload.notes
        adj.updated_at = datetime.now()  # noqa: DTZ005
        session.flush()
        return _serialize(adj)


@router.delete("/adjustments/{adjustment_id}")
def delete_adjustment(
    adjustment_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    with session_scope() as session:
        adj = session.get(StudentFeeAdjustment, adjustment_id)
        if not adj:
            raise HTTPException(status_code=404, detail="折抵紀錄不存在")
        if not is_unrestricted(current_user):
            assert_student_access(session, current_user, adj.student_id)
        session.delete(adj)
        return {"deleted": adjustment_id}
