"""api/fees/templates.py — 費用範本 CRUD

涵蓋 fee_templates 表（學年/學期/年級/費用類型四欄唯一鍵）的 list/create/update/delete。
範本驅動的批量產生位於 generation.py。
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from models.base import session_scope
from models.fees import FeeTemplate
from utils.auth import require_staff_permission
from utils.finance_guards import require_finance_approve
from utils.permissions import Permission

from ._helpers import (
    FEE_PAYMENT_APPROVAL_THRESHOLD,
    FeeTemplateCreate,
    FeeTemplateUpdate,
)

router = APIRouter()


def _validate_template_breakdown(amount: int, breakdown: Optional[dict]) -> None:
    """月費 breakdown 各鍵總和需 == amount,否則拒絕。"""
    if not breakdown:
        return
    total = sum(int(v) for v in breakdown.values())
    if total != amount:
        raise HTTPException(
            status_code=400,
            detail=f"breakdown 總和 {total} 與 amount {amount} 不符",
        )


def _template_to_dict(t: FeeTemplate) -> dict:
    return {
        "id": t.id,
        "grade_id": t.grade_id,
        "school_year": t.school_year,
        "semester": t.semester,
        "fee_type": t.fee_type,
        "name": t.name,
        "amount": t.amount,
        "breakdown": t.breakdown,
        "due_date_offset_days": t.due_date_offset_days,
        "is_active": t.is_active,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


@router.get("/templates")
def list_fee_templates(
    school_year: Optional[int] = Query(None),
    semester: Optional[int] = Query(None, ge=1, le=2),
    fee_type: Optional[str] = Query(
        None, pattern="^(registration|miscellaneous|monthly)$"
    ),
    is_active: Optional[bool] = Query(None),
    current_user: dict = Depends(require_staff_permission(Permission.FEES_READ)),
):
    with session_scope() as session:
        q = session.query(FeeTemplate)
        if school_year is not None:
            q = q.filter(FeeTemplate.school_year == school_year)
        if semester is not None:
            q = q.filter(FeeTemplate.semester == semester)
        if fee_type is not None:
            q = q.filter(FeeTemplate.fee_type == fee_type)
        if is_active is not None:
            q = q.filter(FeeTemplate.is_active == is_active)
        items = q.order_by(
            FeeTemplate.school_year.desc(),
            FeeTemplate.semester,
            FeeTemplate.grade_id,
            FeeTemplate.fee_type,
        ).all()
        return [_template_to_dict(t) for t in items]


@router.post("/templates")
def create_fee_template(
    payload: FeeTemplateCreate,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    _validate_template_breakdown(payload.amount, payload.breakdown)
    require_finance_approve(
        payload.amount,
        current_user,
        threshold=FEE_PAYMENT_APPROVAL_THRESHOLD,
        action_label="建立費用範本（單筆金額）",
    )
    with session_scope() as session:
        existing = (
            session.query(FeeTemplate)
            .filter(
                FeeTemplate.grade_id == payload.grade_id,
                FeeTemplate.school_year == payload.school_year,
                FeeTemplate.semester == payload.semester,
                FeeTemplate.fee_type == payload.fee_type,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"已存在範本(grade={payload.grade_id} "
                    f"{payload.school_year}-{payload.semester} {payload.fee_type})"
                ),
            )
        t = FeeTemplate(
            grade_id=payload.grade_id,
            school_year=payload.school_year,
            semester=payload.semester,
            fee_type=payload.fee_type,
            name=payload.name,
            amount=payload.amount,
            breakdown=payload.breakdown,
            due_date_offset_days=payload.due_date_offset_days,
            is_active=payload.is_active,
            created_by=current_user.get("username"),
            updated_by=current_user.get("username"),
        )
        session.add(t)
        session.flush()
        result = _template_to_dict(t)

        request.state.audit_entity_id = str(t.id)
        request.state.audit_summary = f"建立費用範本 {t.name}"
        request.state.audit_changes = {
            "action": "fee_template_create",
            "template": result,
        }
        return result


@router.put("/templates/{template_id}")
def update_fee_template(
    template_id: int,
    payload: FeeTemplateUpdate,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    with session_scope() as session:
        t = session.query(FeeTemplate).filter(FeeTemplate.id == template_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="範本不存在")
        before = _template_to_dict(t)
        new_amount = payload.amount if payload.amount is not None else t.amount
        new_breakdown = (
            payload.breakdown if payload.breakdown is not None else t.breakdown
        )
        _validate_template_breakdown(new_amount, new_breakdown)
        # 範本金額守衛：新值/舊值/變動量 任一超過金流門檻即需 ACTIVITY_PAYMENT_APPROVE。
        # Why: 範本 amount 任一面向超門檻都會在 /generate 時放大為全班全月寫入。
        # - 新值 > 門檻：直接建貴範本（含「慢漲」一次到位）
        # - 舊值 > 門檻：對大額範本的任何修改（含降價、breakdown 替換）都應審視
        # - |變動| > 門檻：避免一次性大跳，含「降到 0 等同停收」走 update 而非 delete
        # 三條件合起來封死「分多次 delta < 門檻 慢漲」與「降到 0 靜默免收」兩條路。
        old_amount = int(t.amount or 0)
        new_amount_int = int(new_amount or 0)
        guard_basis = max(new_amount_int, old_amount, abs(new_amount_int - old_amount))
        if guard_basis > FEE_PAYMENT_APPROVAL_THRESHOLD:
            require_finance_approve(
                guard_basis,
                current_user,
                threshold=FEE_PAYMENT_APPROVAL_THRESHOLD,
                action_label="調整費用範本（新值/舊值/變動 任一超門檻）",
            )
        if payload.name is not None:
            t.name = payload.name
        if payload.amount is not None:
            t.amount = payload.amount
        if payload.breakdown is not None:
            t.breakdown = payload.breakdown
        if payload.due_date_offset_days is not None:
            t.due_date_offset_days = payload.due_date_offset_days
        if payload.is_active is not None:
            t.is_active = payload.is_active
        t.updated_by = current_user.get("username")
        session.flush()
        after = _template_to_dict(t)

        request.state.audit_entity_id = str(template_id)
        request.state.audit_summary = f"編輯費用範本 {t.name}"
        request.state.audit_changes = {
            "action": "fee_template_update",
            "before": before,
            "after": after,
        }
        return after


@router.delete("/templates/{template_id}")
def delete_fee_template(
    template_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """軟刪除(is_active=False),保留歷史記錄。"""
    with session_scope() as session:
        t = session.query(FeeTemplate).filter(FeeTemplate.id == template_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="範本不存在")
        t.is_active = False
        t.updated_by = current_user.get("username")
        session.flush()

        request.state.audit_entity_id = str(template_id)
        request.state.audit_summary = f"停用費用範本 {t.name}"
        request.state.audit_changes = {
            "action": "fee_template_delete",
            "template_id": template_id,
        }
        return {"ok": True, "template_id": template_id}
