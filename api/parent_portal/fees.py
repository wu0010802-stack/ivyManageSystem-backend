"""api/parent_portal/fees.py — 家長端費用查詢（read-only）。

- GET /api/parent/fees/summary：跨子女總覽（未繳/已繳/即將到期/逾期）
- GET /api/parent/fees/records：列某學生某學期費用記錄
- GET /api/parent/fees/records/{id}/payments：收據（以 idempotency_key 分組）

隱私：refunded_by / operator 等員工欄位不對家長揭露。
"""

from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import get_session
from models.fees import StudentFeePayment, StudentFeeRecord, StudentFeeRefund
from utils.auth import require_parent_role

from ._shared import _assert_student_owned, _get_parent_student_ids

router = APIRouter(prefix="/fees", tags=["parent-fees"])


_DUE_SOON_DAYS = 7


def compute_fees_summary(session, student_ids: list[int]) -> dict:
    """計算跨子女費用總覽。

    可被 home/summary 等彙總端點重用。
    回傳 {"by_student": [...], "totals": {..., "outstanding_count": N}}。
    `outstanding_count` 為仍有 outstanding 的 record 筆數，方便首頁顯示「未繳 N 筆」。
    """
    if not student_ids:
        return {"by_student": [], "totals": _empty_summary()}

    records = (
        session.query(StudentFeeRecord)
        .filter(StudentFeeRecord.student_id.in_(student_ids))
        .all()
    )

    today = date.today()
    soon = today + timedelta(days=_DUE_SOON_DAYS)
    by_student: dict[int, dict] = defaultdict(_empty_totals)

    total_due = 0
    total_paid = 0
    total_outstanding = 0
    total_overdue = 0
    total_due_soon = 0
    outstanding_count = 0

    for r in records:
        outstanding = max(0, (r.amount_due or 0) - (r.amount_paid or 0))
        entry = by_student[r.student_id]
        entry["amount_due"] += r.amount_due or 0
        entry["amount_paid"] += r.amount_paid or 0
        entry["outstanding"] += outstanding
        if outstanding > 0:
            outstanding_count += 1
            if r.due_date is not None:
                if r.due_date < today:
                    entry["overdue"] += outstanding
                    total_overdue += outstanding
                elif r.due_date <= soon:
                    entry["due_soon"] += outstanding
                    total_due_soon += outstanding
        total_due += r.amount_due or 0
        total_paid += r.amount_paid or 0
        total_outstanding += outstanding

    return {
        "by_student": [
            {"student_id": sid, **stats} for sid, stats in by_student.items()
        ],
        "totals": {
            "amount_due": total_due,
            "amount_paid": total_paid,
            "outstanding": total_outstanding,
            "overdue": total_overdue,
            "due_soon": total_due_soon,
            "outstanding_count": outstanding_count,
        },
    }


@router.get("/summary")
def fees_summary(current_user: dict = Depends(require_parent_role())):
    """跨子女費用總覽（依學生分組）。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        _, student_ids = _get_parent_student_ids(session, user_id)
        return compute_fees_summary(session, student_ids)
    finally:
        session.close()


@router.get("/records")
def list_records(
    student_id: int = Query(..., gt=0),
    period: Optional[str] = Query(None),
    current_user: dict = Depends(require_parent_role()),
):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        _assert_student_owned(session, user_id, student_id)
        q = session.query(StudentFeeRecord).filter(
            StudentFeeRecord.student_id == student_id
        )
        if period:
            q = q.filter(StudentFeeRecord.period == period)
        rows = q.order_by(
            StudentFeeRecord.due_date.asc().nulls_last(),
            StudentFeeRecord.created_at.asc(),
        ).all()
        items = [
            {
                "id": r.id,
                "fee_item_name": r.fee_item_name,
                "period": r.period,
                "amount_due": r.amount_due or 0,
                "amount_paid": r.amount_paid or 0,
                "outstanding": max(0, (r.amount_due or 0) - (r.amount_paid or 0)),
                "status": r.status,
                "due_date": r.due_date.isoformat() if r.due_date else None,
                "payment_date": r.payment_date.isoformat() if r.payment_date else None,
            }
            for r in rows
        ]
        return {"items": items, "total": len(items)}
    finally:
        session.close()


@router.get("/records/{record_id}/payments")
def list_payments(
    record_id: int,
    current_user: dict = Depends(require_parent_role()),
):
    """收據以 idempotency_key 分組（同一筆收據可能含多次付款）。

    隱私：operator / refunded_by 等員工欄位不回傳；refund 只回金額與原因。
    """
    user_id = current_user["user_id"]
    session = get_session()
    try:
        record = (
            session.query(StudentFeeRecord)
            .filter(StudentFeeRecord.id == record_id)
            .first()
        )
        if record is None:
            raise HTTPException(status_code=404, detail="找不到費用記錄")
        _assert_student_owned(session, user_id, record.student_id)

        payments = (
            session.query(StudentFeePayment)
            .filter(StudentFeePayment.record_id == record_id)
            .order_by(StudentFeePayment.payment_date.asc(), StudentFeePayment.id.asc())
            .all()
        )
        refunds = (
            session.query(StudentFeeRefund)
            .filter(StudentFeeRefund.record_id == record_id)
            .order_by(StudentFeeRefund.refunded_at.asc(), StudentFeeRefund.id.asc())
            .all()
        )
        return {
            "fee_item_name": record.fee_item_name,
            "period": record.period,
            "payments": [
                {
                    "amount": p.amount,
                    "payment_date": (
                        p.payment_date.isoformat() if p.payment_date else None
                    ),
                    "payment_method": p.payment_method,
                    "receipt_no": p.idempotency_key,  # 對家長以「收據編號」呈現
                }
                for p in payments
            ],
            "refunds": [
                {
                    "amount": r.amount,
                    "reason": r.reason,
                    "refunded_at": r.refunded_at.isoformat() if r.refunded_at else None,
                }
                for r in refunds
            ],
        }
    finally:
        session.close()


def _empty_totals() -> dict:
    """單一學生的 stats（by_student 每筆 entry 用）。"""
    return {
        "amount_due": 0,
        "amount_paid": 0,
        "outstanding": 0,
        "overdue": 0,
        "due_soon": 0,
    }


def _empty_summary() -> dict:
    """fees summary 整體 totals（含 outstanding_count）。"""
    return {**_empty_totals(), "outstanding_count": 0}
