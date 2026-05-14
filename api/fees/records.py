"""api/fees/records.py — 學期清單、學費紀錄查詢/繳費/摘要。

c2 後：FeeItem CRUD（/items 4 endpoints）已退場；/periods 改從
student_fee_records.period 取 distinct（fee_items 表已 DROP）。
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError

from models.base import session_scope
from models.fees import StudentFeePayment, StudentFeeRecord
from utils.audit import write_audit_in_session
from utils.auth import require_staff_permission
from utils.finance_guards import require_finance_approve
from utils.permissions import Permission
from utils.portfolio_access import assert_student_access, is_unrestricted

from ._helpers import (
    FEE_PAYMENT_APPROVAL_THRESHOLD,
    PayRequest,
    _apply_fee_record_filters,
    _invalidate_finance_summary_cache,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# 學期清單
# ---------------------------------------------------------------------------


@router.get("/periods")
def list_fee_periods(
    _: None = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """取得所有已建立的學期列表（供前端下拉選單使用）。

    c2 後改從 student_fee_records.period 取 distinct（fee_items 表已 DROP）。
    """
    with session_scope() as session:
        rows = (
            session.query(StudentFeeRecord.period)
            .distinct()
            .order_by(StudentFeeRecord.period.desc())
            .all()
        )
        return [r.period for r in rows if r.period]


# ---------------------------------------------------------------------------
# 費用記錄查詢（含分頁）
# ---------------------------------------------------------------------------


@router.get("/records")
def list_fee_records(
    period: Optional[str] = Query(None),
    classroom_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None, pattern="^(unpaid|partial|paid)$"),
    student_name: Optional[str] = Query(None),
    student_id: Optional[int] = Query(None, gt=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """查詢費用記錄（支援分頁）。

    student_id：指定學生 ID 時，僅回傳該學生的費用紀錄（跨學期）。
    """
    with session_scope() as session:
        # F-034：班級 scope 守衛 — 非 admin/hr/supervisor caller 必須帶
        # student_id 並通過 assert_student_access；不帶 student_id 全校列出
        # 一律拒絕，避免「自訂財務角色」拿全校學生繳費明細。
        if not is_unrestricted(current_user):
            if student_id is None:
                raise HTTPException(
                    status_code=403,
                    detail="非管理角色不得列出全校繳費紀錄，請指定 student_id",
                )
            assert_student_access(session, current_user, student_id)
        q = _apply_fee_record_filters(
            session.query(StudentFeeRecord),
            period=period,
            classroom_name=classroom_name,
            status=status,
            student_name=student_name,
            student_id=student_id,
        )

        total = q.count()
        records = (
            q.order_by(
                StudentFeeRecord.period.desc(),
                StudentFeeRecord.classroom_name,
                StudentFeeRecord.student_name,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [
                {
                    "id": r.id,
                    "student_id": r.student_id,
                    "student_name": r.student_name,
                    "classroom_name": r.classroom_name,
                    "fee_item_name": r.fee_item_name,
                    "amount_due": r.amount_due,
                    "amount_paid": r.amount_paid,
                    "status": r.status,
                    "payment_date": (
                        r.payment_date.isoformat() if r.payment_date else None
                    ),
                    "payment_method": r.payment_method,
                    "notes": r.notes,
                    "period": r.period,
                }
                for r in records
            ],
        }


# ---------------------------------------------------------------------------
# 登記繳費
# ---------------------------------------------------------------------------


@router.put("/records/{record_id}/pay")
def pay_fee_record(
    record_id: int,
    payload: PayRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """登記繳費 — API 契約保留「累計已繳」語意，底層改為 append-only 流水。

    Why: 財務月報過去用 StudentFeeRecord.payment_date / status 聚合，分期收款
    會把前期收入搬到最後一次付款月份，退款後月份可能整筆消失。現在每次 pay
    都會 INSERT 一筆 StudentFeePayment（delta 金額 + 本次付款日），財報改
    SUM 流水表即可正確歸月。

    - payload.amount_paid 仍代表「累計到此值」，後端自動算 delta 插入
    - delta < 0 拒絕（走退款流程）；delta = 0 視為只更新 method/notes 快照
    - record 上的 amount_paid / payment_date / payment_method 保持「最後一次」
      快照供清單顯示；真正的月度聚合看 StudentFeePayment
    - idempotency_key：全域唯一，同 key 重送回放（DB UNIQUE 兜底）
    """

    def _assert_pay_payload_matches(session, hit: StudentFeePayment, record_id: int):
        """同 key 必須對應完整相同的 payload 上下文（record_id + payment_date +
        payment_method + 目標 amount_paid）；任一欄位不符視為 key 誤用 → 409。

        Why: 若只驗 record_id，同 record 誤帶舊 key + 新 amount 會誤 replay，
        呼叫端以為已登記但實際沒新增流水，導致資料掉筆。
        """
        mismatch = []
        if hit.record_id != record_id:
            mismatch.append(f"record_id（已用於 {hit.record_id}）")
        if hit.payment_date != payload.payment_date:
            mismatch.append(f"payment_date（原 {hit.payment_date}）")
        if hit.payment_method != payload.payment_method:
            mismatch.append(f"payment_method（原 {hit.payment_method}）")
        # 推算 hit 建立當下 record 的累計已繳 = SUM(payments WHERE id <= hit.id)
        hit_cumulative = (
            session.query(func.coalesce(func.sum(StudentFeePayment.amount), 0))
            .filter(
                StudentFeePayment.record_id == hit.record_id,
                StudentFeePayment.id <= hit.id,
            )
            .scalar()
        ) or 0
        if payload.amount_paid is not None and int(payload.amount_paid) != int(
            hit_cumulative
        ):
            mismatch.append(
                f"amount_paid（原累計 NT${hit_cumulative}，本次 NT${payload.amount_paid}）"
            )
        if mismatch:
            raise HTTPException(
                status_code=409,
                detail="idempotency_key 與先前請求的 payload 不符："
                + "、".join(mismatch),
            )

    with session_scope() as session:
        # ── 冪等性重送檢查：先於任何寫入 ─────────────────────────────
        if payload.idempotency_key:
            hit = (
                session.query(StudentFeePayment)
                .filter(StudentFeePayment.idempotency_key == payload.idempotency_key)
                .first()
            )
            if hit is not None:
                _assert_pay_payload_matches(session, hit, record_id)
                rec = (
                    session.query(StudentFeeRecord)
                    .filter(StudentFeeRecord.id == record_id)
                    .first()
                )
                return {
                    "ok": True,
                    "amount_paid": rec.amount_paid if rec else None,
                    "previous_amount_paid": (rec.amount_paid if rec else 0)
                    - hit.amount,
                    "idempotent_replay": True,
                }

        record = (
            session.query(StudentFeeRecord)
            .filter(StudentFeeRecord.id == record_id)
            .with_for_update()
            .first()
        )
        if not record:
            raise HTTPException(status_code=404, detail="費用記錄不存在")
        if record.status == "paid":
            raise HTTPException(status_code=400, detail="此記錄已完成繳費")

        amount_paid = (
            payload.amount_paid
            if payload.amount_paid is not None
            else record.amount_due
        )
        if amount_paid > record.amount_due:
            raise HTTPException(
                status_code=400,
                detail=f"繳費金額（{amount_paid}）不得超過應繳金額（{record.amount_due}）",
            )

        previous_paid = record.amount_paid or 0
        if amount_paid < previous_paid:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"新金額 NT${amount_paid} 低於已登記金額 NT${previous_paid}，"
                    "請改用退款流程（POST /records/{id}/refund）"
                ),
            )

        delta = amount_paid - previous_paid
        operator = current_user.get("username", "") or "unknown"

        # ── A 錢守衛:本次入帳 delta 超 FEE_PAYMENT_APPROVAL_THRESHOLD 需金流簽核 ──
        # Why: 舊版 FEES_WRITE 即可登記 NT$999,999 為現金收入,財報直接受影響。
        # 用本次 delta(非累計)判斷:讓常規月費可走、學期/年費等大筆需 approver。
        if delta > 0:
            require_finance_approve(
                delta,
                current_user,
                threshold=FEE_PAYMENT_APPROVAL_THRESHOLD,
                action_label="學費單筆繳款",
            )

        # Append-only 流水：delta > 0 時才寫一筆（delta=0 只更新快照）
        if delta > 0:
            payment = StudentFeePayment(
                record_id=record.id,
                amount=delta,
                payment_date=payload.payment_date,
                payment_method=payload.payment_method,
                notes=payload.notes or "",
                operator=operator,
                idempotency_key=payload.idempotency_key,
            )
            session.add(payment)

        record.amount_paid = amount_paid
        record.payment_date = payload.payment_date
        record.payment_method = payload.payment_method
        record.notes = payload.notes or ""
        record.status = "paid" if amount_paid >= record.amount_due else "partial"
        record.updated_at = datetime.now()

        student_name = record.student_name

        # DB 層 UNIQUE 攔下並發同 key 的第二筆：轉為 replay
        # 和前置檢查共用 _assert_pay_payload_matches，不可放寬檢查力道
        try:
            session.flush()
        except IntegrityError as e:
            session.rollback()
            if (
                payload.idempotency_key
                and "idempotency_key" in str(getattr(e, "orig", e)).lower()
            ):
                with session_scope() as replay_session:
                    hit = (
                        replay_session.query(StudentFeePayment)
                        .filter(
                            StudentFeePayment.idempotency_key == payload.idempotency_key
                        )
                        .first()
                    )
                    if hit is not None:
                        _assert_pay_payload_matches(replay_session, hit, record_id)
                        rec = (
                            replay_session.query(StudentFeeRecord)
                            .filter(StudentFeeRecord.id == record_id)
                            .first()
                        )
                        return {
                            "ok": True,
                            "amount_paid": rec.amount_paid if rec else None,
                            "previous_amount_paid": (
                                (rec.amount_paid if rec else 0) - hit.amount
                            ),
                            "idempotent_replay": True,
                        }
            raise

        # 同交易 outbox：AuditLog 必須與金流變動共生死。
        # Why: 過去走 middleware fire-and-forget；threadpool/DB 短路時 audit 會丟，
        # 但學費紀錄已 commit。改寫在此 session 後，audit 失敗整個 rollback。
        write_audit_in_session(
            session,
            request,
            action="UPDATE",
            entity_type="fee",
            entity_id=record_id,
            summary=(
                f"繳費登記 {record.period or ''} {student_name}: "
                f"NT${previous_paid} → NT${amount_paid}（本次 +NT${delta}）"
                f"（{payload.payment_method}，by {operator}）"
            ),
            changes={
                "action": "fee_pay",
                "record_id": record_id,
                "student_id": record.student_id,
                "student_name": student_name,
                "period": record.period,
                "previous_paid": previous_paid,
                "new_paid": amount_paid,
                "delta": delta,
                "amount_due": record.amount_due,
                "status_after": record.status,
                "payment_method": payload.payment_method,
                "payment_date": payload.payment_date.isoformat(),
                "payment_id": payment.id if delta > 0 else None,
                "idempotency_key": payload.idempotency_key,
                "operator": operator,
            },
        )

    # session_scope commit 後失效報表快取
    _invalidate_finance_summary_cache()

    # 金額變動 warning 保留一份（AuditLog 寫失敗時仍有日誌可查）
    if delta != 0:
        logger.warning(
            "FEE_PAY_CHANGE record_id=%s student=%s operator=%s prev=%s new=%s delta=%s method=%s",
            record_id,
            student_name,
            operator,
            previous_paid,
            amount_paid,
            delta,
            payload.payment_method,
        )
    return {
        "ok": True,
        "amount_paid": amount_paid,
        "previous_amount_paid": previous_paid,
        "delta": delta,
    }


# ---------------------------------------------------------------------------
# 統計摘要
# ---------------------------------------------------------------------------


@router.get("/summary")
def fee_summary(
    period: Optional[str] = Query(None),
    classroom_name: Optional[str] = Query(None),
    status: Optional[str] = Query(None, pattern="^(unpaid|partial|paid)$"),
    student_name: Optional[str] = Query(None),
    _: None = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """統計摘要：總應繳金額、已繳、未繳人數/金額"""
    with session_scope() as session:
        q = _apply_fee_record_filters(
            session.query(StudentFeeRecord),
            period=period,
            classroom_name=classroom_name,
            status=status,
            student_name=student_name,
        )

        agg_q = q.with_entities(
            func.count(StudentFeeRecord.id).label("total_count"),
            func.coalesce(
                func.sum(case((StudentFeeRecord.status == "paid", 1), else_=0)), 0
            ).label("paid_count"),
            func.coalesce(
                func.sum(case((StudentFeeRecord.status == "partial", 1), else_=0)), 0
            ).label("partial_count"),
            func.coalesce(func.sum(StudentFeeRecord.amount_due), 0).label("total_due"),
            func.coalesce(func.sum(StudentFeeRecord.amount_paid), 0).label(
                "total_paid"
            ),
        )
        row = agg_q.one()
        total_count = row.total_count or 0
        paid_count = int(row.paid_count or 0)
        partial_count = int(row.partial_count or 0)
        total_due = int(row.total_due or 0)
        total_paid = int(row.total_paid or 0)

        return {
            "total_count": total_count,
            "paid_count": paid_count,
            "partial_count": partial_count,
            "unpaid_count": total_count - paid_count - partial_count,
            "total_due": total_due,
            "total_paid": total_paid,
            "total_unpaid": total_due - total_paid,
        }
