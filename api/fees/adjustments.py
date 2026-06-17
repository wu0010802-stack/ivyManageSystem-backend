"""api/fees/adjustments.py — 學費折抵 CRUD（同胞優惠 / 預繳 / 請假扣款 / 其他）。

折抵類獨立於 StudentFeeRecord（正金額應收）；套用方式：
    total_due_adjusted = SUM(records.amount_due) - SUM(adjustments.amount)

不影響 amount_paid / payments 流水；折抵刪除即還原。
"""

import logging
from datetime import datetime
from utils.taipei_time import now_taipei_naive
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func

from models.base import session_scope
from models.fees import StudentFeeAdjustment
from utils.auth import require_staff_permission
from utils.finance_guards import require_adjustment_reason, require_finance_approve
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


def _period_adjustment_total(
    session, student_id: int, period: str, *, exclude_id: Optional[int] = None
) -> int:
    """該生該學期既有折抵金額合計（可排除某筆，供 update 重算）。

    用於累積金流簽核：避免把一筆大額折抵拆成多筆 ≤ 閾值繞過 require_finance_approve。
    """
    q = session.query(func.coalesce(func.sum(StudentFeeAdjustment.amount), 0)).filter(
        StudentFeeAdjustment.student_id == student_id,
        StudentFeeAdjustment.period == period,
    )
    if exclude_id is not None:
        q = q.filter(StudentFeeAdjustment.id != exclude_id)
    return int(q.scalar() or 0)


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
        raise HTTPException(
            status_code=400, detail=f"非法 adjustment_type: {adjustment_type}"
        )

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
        items = q.order_by(
            StudentFeeAdjustment.period.desc(),
            StudentFeeAdjustment.student_id,
            StudentFeeAdjustment.id,
        ).all()
        return {"items": [_serialize(a) for a in items], "total": len(items)}


@router.post("/adjustments")
def create_adjustment(
    payload: AdjustmentCreate,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    with session_scope() as session:
        if not is_unrestricted(current_user):
            assert_student_access(session, current_user, payload.student_id)

        # ── A 錢守衛 ──────────────────────────────────────────────────
        # 折抵直接抵減未繳金額（_helpers.compute_fee_summary / 家長端 summary），
        # 等同退款的金流效果，故與退款端點同套守衛：原因必填 + 累積簽核。
        # 累積以「該生該學期既有折抵 + 本次」判斷，防拆筆繞過閾值。
        payload.reason = require_adjustment_reason(payload.reason)
        prior = _period_adjustment_total(session, payload.student_id, payload.period)
        require_finance_approve(
            prior + payload.amount, current_user, action_label="學費折抵累積"
        )

        adj = StudentFeeAdjustment(
            student_id=payload.student_id,
            period=payload.period,
            adjustment_type=payload.adjustment_type,
            amount=payload.amount,
            reason=payload.reason,
            notes=payload.notes or "",
            created_by=current_user.get("username"),
            created_at=now_taipei_naive(),
            updated_at=now_taipei_naive(),
        )
        session.add(adj)
        session.flush()
        logger.info(
            "fee adjustment created id=%s student_id=%s period=%s type=%s amount=%s",
            adj.id,
            adj.student_id,
            adj.period,
            adj.adjustment_type,
            adj.amount,
        )
        request.state.audit_summary = (
            f"新增學費折抵：student_id={adj.student_id} period={adj.period} "
            f"type={adj.adjustment_type} amount={adj.amount}"
        )
        request.state.audit_changes = {
            "student_id": adj.student_id,
            "period": adj.period,
            "adjustment_type": adj.adjustment_type,
            "amount": adj.amount,
            "reason": adj.reason,
        }
        return _serialize(adj)


@router.put("/adjustments/{adjustment_id}")
def update_adjustment(
    adjustment_id: int,
    payload: AdjustmentUpdate,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    with session_scope() as session:
        adj = session.get(StudentFeeAdjustment, adjustment_id)
        if not adj:
            raise HTTPException(status_code=404, detail="折抵紀錄不存在")
        if not is_unrestricted(current_user):
            assert_student_access(session, current_user, adj.student_id)

        before = {
            "adjustment_type": adj.adjustment_type,
            "amount": adj.amount,
            "reason": adj.reason,
        }

        # ── A 錢守衛 ──────────────────────────────────────────────────
        # 提供原因即驗證 ≥ 5 字；變更金額時以「該生該學期其餘折抵 + 新金額」
        # 累積判斷簽核，防把大額折抵藏進 update。
        if payload.reason is not None:
            payload.reason = require_adjustment_reason(payload.reason)
        if payload.amount is not None:
            prior = _period_adjustment_total(
                session, adj.student_id, adj.period, exclude_id=adj.id
            )
            require_finance_approve(
                prior + payload.amount,
                current_user,
                action_label="學費折抵調整累積",
            )

        if payload.adjustment_type is not None:
            adj.adjustment_type = payload.adjustment_type
        if payload.amount is not None:
            adj.amount = payload.amount
        if payload.reason is not None:
            adj.reason = payload.reason
        if payload.notes is not None:
            adj.notes = payload.notes
        adj.updated_at = now_taipei_naive()
        session.flush()
        request.state.audit_summary = (
            f"更新學費折抵 id={adj.id}：student_id={adj.student_id} "
            f"period={adj.period} amount={before['amount']}→{adj.amount}"
        )
        request.state.audit_changes = {
            "id": adj.id,
            "student_id": adj.student_id,
            "period": adj.period,
            "before": before,
            "after": {
                "adjustment_type": adj.adjustment_type,
                "amount": adj.amount,
                "reason": adj.reason,
            },
        }
        return _serialize(adj)


@router.delete("/adjustments/{adjustment_id}")
def delete_adjustment(
    adjustment_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    with session_scope() as session:
        adj = session.get(StudentFeeAdjustment, adjustment_id)
        if not adj:
            raise HTTPException(status_code=404, detail="折抵紀錄不存在")
        if not is_unrestricted(current_user):
            assert_student_access(session, current_user, adj.student_id)

        # ── A 錢守衛 ──────────────────────────────────────────────────
        # 刪除折抵 = 還原該筆應收（反向金流，效果等同重新加回應繳金額），
        # 與建立/更新同屬金流動作，故對稱補金流簽核：大額折抵的刪除需
        # ACTIVITY_PAYMENT_APPROVE，避免無簽核權者單方面抹除大額折抵。
        require_finance_approve(adj.amount, current_user, action_label="刪除學費折抵")

        # 硬刪即還原（model 設計），刪除前留同交易 audit 快照供鑑識
        request.state.audit_summary = (
            f"刪除學費折抵 id={adj.id}：student_id={adj.student_id} "
            f"period={adj.period} type={adj.adjustment_type} amount={adj.amount}"
        )
        request.state.audit_changes = {
            "deleted_id": adj.id,
            "student_id": adj.student_id,
            "period": adj.period,
            "adjustment_type": adj.adjustment_type,
            "amount": adj.amount,
            "reason": adj.reason,
        }
        session.delete(adj)
        return {"deleted": adjustment_id}
