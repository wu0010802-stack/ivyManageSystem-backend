"""api/parent_portal/fees.py — 家長端費用查詢（read-only）。

- GET /api/parent/fees/summary：跨子女總覽（未繳/已繳/即將到期/逾期）
- GET /api/parent/fees/records：列某學生某學期費用記錄
- GET /api/parent/fees/records/{id}/payments：收據（以 idempotency_key 分組）

隱私：refunded_by / operator 等員工欄位不對家長揭露。
"""

from collections import defaultdict
from datetime import date, timedelta
from utils.taipei_time import today_taipei
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.fees import (
    StudentFeeAdjustment,
    StudentFeePayment,
    StudentFeeRecord,
    StudentFeeRefund,
)
from services.business_errors.parent import ParentNotAuthorized
from utils.auth import require_parent_role

from ._dependencies import get_parent_db
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

    today = today_taipei()
    soon = today + timedelta(days=_DUE_SOON_DAYS)

    # 先按 (student_id, period) 分桶，折抵才能 scope 在同學期內套用。
    # Why: StudentFeeAdjustment 設計為「該生該學期」折抵（見 model docstring），
    # 後台 summary 已按 (student_id, period) scope（test_fees.TestFeeSummaryAdjustmentScope）。
    # 家長端原本只按 student_id 加總折抵再 overdue-first 扣，會讓 114-2 的折抵先吃掉
    # 114-1 的逾期欠款，低估逾期金額。
    buckets: dict[tuple[int, str], dict] = defaultdict(_empty_totals)
    outstanding_count = 0

    for r in records:
        outstanding = max(0, (r.amount_due or 0) - (r.amount_paid or 0))
        bucket = buckets[(r.student_id, r.period)]
        bucket["amount_due"] += r.amount_due or 0
        bucket["amount_paid"] += r.amount_paid or 0
        bucket["outstanding"] += outstanding
        if outstanding > 0:
            outstanding_count += 1
            if r.due_date is not None:
                if r.due_date < today:
                    bucket["overdue"] += outstanding
                elif r.due_date <= soon:
                    bucket["due_soon"] += outstanding

    # 折抵聚合：按 (student_id, period) 加總，僅在同學期桶內 overdue→due_soon→outstanding
    # 扣抵；amount_due 同步減（保留 amount_paid 流水不動）。
    # 註：若某學期有折抵但尚無 record（如預繳未產生帳單），折抵不會套用到其他學期，
    # 僅以 adjustment 數字呈現（符合「未產生帳單的預繳不抵減逾期」語意）。
    adj_rows = (
        session.query(
            StudentFeeAdjustment.student_id,
            StudentFeeAdjustment.period,
            func.coalesce(func.sum(StudentFeeAdjustment.amount), 0),
        )
        .filter(StudentFeeAdjustment.student_id.in_(student_ids))
        .group_by(StudentFeeAdjustment.student_id, StudentFeeAdjustment.period)
        .all()
    )
    for sid, period, adj_total in adj_rows:
        adj = int(adj_total or 0)
        if adj <= 0:
            continue
        bucket = buckets[(sid, period)]
        bucket["adjustment"] = bucket.get("adjustment", 0) + adj
        remaining = adj
        for k in ("overdue", "due_soon", "outstanding"):
            take = min(bucket[k], remaining)
            bucket[k] -= take
            remaining -= take
        bucket["amount_due"] = max(0, bucket["amount_due"] - adj)

    # 桶 → 依學生上捲為 by_student，再加總 grand totals
    by_student: dict[int, dict] = defaultdict(_empty_totals)
    for (sid, _period), bucket in buckets.items():
        entry = by_student[sid]
        for k in (
            "amount_due",
            "amount_paid",
            "outstanding",
            "overdue",
            "due_soon",
            "adjustment",
        ):
            entry[k] += bucket.get(k, 0)

    return {
        "by_student": [
            {"student_id": sid, **stats} for sid, stats in by_student.items()
        ],
        "totals": {
            "amount_due": sum(e["amount_due"] for e in by_student.values()),
            "amount_paid": sum(e["amount_paid"] for e in by_student.values()),
            "outstanding": sum(e["outstanding"] for e in by_student.values()),
            "overdue": sum(e["overdue"] for e in by_student.values()),
            "due_soon": sum(e["due_soon"] for e in by_student.values()),
            "outstanding_count": outstanding_count,
            "adjustment": sum(e["adjustment"] for e in by_student.values()),
        },
    }


@router.get("/summary")
def fees_summary(
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    """跨子女費用總覽（依學生分組）。"""
    user_id = current_user["user_id"]
    _, student_ids = _get_parent_student_ids(session, user_id)
    return compute_fees_summary(session, student_ids)


@router.get("/records")
def list_records(
    student_id: int = Query(..., gt=0),
    period: Optional[str] = Query(None),
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    user_id = current_user["user_id"]
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


@router.get("/records/{record_id}/payments")
def list_payments(
    record_id: int,
    current_user: dict = Depends(require_parent_role()),
    session: Session = Depends(get_parent_db),
):
    """收據以 idempotency_key 分組（同一筆收據可能含多次付款）。

    隱私：operator / refunded_by 等員工欄位不回傳；refund 只回金額與原因。
    """
    user_id = current_user["user_id"]
    # F-002：collapse 「記錄不存在」與「不屬於本家庭」為同一 403，
    # 避免攻擊者透過 status code 差異枚舉 fee record id 存在性。
    _, owned_student_ids = _get_parent_student_ids(session, user_id)
    record = (
        session.query(StudentFeeRecord).filter(StudentFeeRecord.id == record_id).first()
    )
    if record is None or record.student_id not in owned_student_ids:
        raise ParentNotAuthorized("查無此資料或無權存取")

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


def _empty_totals() -> dict:
    """單一學生的 stats（by_student 每筆 entry 用）。"""
    return {
        "amount_due": 0,
        "amount_paid": 0,
        "outstanding": 0,
        "overdue": 0,
        "due_soon": 0,
        "adjustment": 0,
    }


def _empty_summary() -> dict:
    """fees summary 整體 totals（含 outstanding_count）。"""
    return {**_empty_totals(), "outstanding_count": 0}
