"""api/fees/refunds.py — 退費建議與退款流程

- POST /records/{id}/refund-suggest：依離園日與費用類型自動計算建議退費
- POST /records/{id}/refund：建立退款紀錄並扣減已繳金額
- GET  /records/{id}/refunds：列出單筆學費紀錄的退款歷史
"""

import logging
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models.base import session_scope
from models.fees import (
    FeeTemplate,
    StudentFeeRecord,
    StudentFeeRefund,
)
from models.student_leave import StudentLeaveRequest
from services.fee_refund_calculator import (
    calc_enrollment_refund,
    calc_monthly_refund,
    longest_consecutive_workdays,
)
from services.workday_rules import classify_day, load_day_rule_maps
from utils.audit import write_audit_in_session
from utils.auth import require_staff_permission
from utils.finance_guards import require_adjustment_reason, require_finance_approve
from utils.permissions import Permission

from ._helpers import (
    RefundRequest,
    RefundSuggestRequest,
    _invalidate_finance_summary_cache,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# 冪等視窗：同 idempotency_key 於視窗內視為重試（避免網路重送導致重複退款）
_REFUND_IDEMPOTENCY_WINDOW_SECONDS = 10 * 60


def _semester_date_range(school_year: int, semester: int) -> tuple[date, date]:
    """民國年+學期 → (start, end) 西元日期。

    上學期: 8/1 ~ 隔年 1/31（學年起始那年 8 月～次年 1 月）
    下學期: 2/1 ~ 7/31（學年起始那年的次年 2 月～7 月）
    """
    western = school_year + 1911
    if semester == 1:
        return date(western, 8, 1), date(western + 1, 1, 31)
    return date(western + 1, 2, 1), date(western + 1, 7, 31)


def _count_workdays(start: date, end: date, holiday_map: dict, makeup_map: dict) -> int:
    """區間內工作日數(排除週末+國定假日,加補班日)。"""
    if end < start:
        return 0
    total = 0
    d = start
    while d <= end:
        info = classify_day(d, holiday_map, makeup_map)
        if info["kind"] == "workday":
            total += 1
        d = d + timedelta(days=1)
    return total


@router.post("/records/{record_id}/refund-suggest")
def suggest_refund(
    record_id: int,
    payload: RefundSuggestRequest,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """根據學生離園日與費用類型,自動計算建議退費金額。

    - registration / miscellaneous → 走 enrollment_ratio
      (T_served/T_total 三段比例 <1/3 退 2/3、1/3..2/3 退 1/3、≥2/3 不退)
    - monthly → 走 monthly_partial
      (事先請假連續 ≥5 上課日, 按 meal+transport 比例退;無 breakdown fallback 全額)
    - material / insurance → no_refund
    - custom / 其他 → manual（不提供自動建議）
    """
    with session_scope() as session:
        rec = (
            session.query(StudentFeeRecord)
            .filter(StudentFeeRecord.id == record_id)
            .first()
        )
        if not rec:
            raise HTTPException(status_code=404, detail="費用記錄不存在")

        fee_type = rec.fee_type or "custom"

        # 代購品 / 保險費 → 不退
        if fee_type in ("material", "insurance"):
            label = "代購品" if fee_type == "material" else "保險費"
            return {
                "suggested_amount": 0,
                "calc_method": "no_refund",
                "calc_payload": {
                    "fee_type": fee_type,
                    "reason": f"{label}依規定不予退費",
                },
                "warnings": [f"{label}依規定不予退費"],
            }

        # 學期區間 (依 period 解析,格式 民國年-學期 e.g. 114-1)
        if not rec.period or "-" not in rec.period:
            raise HTTPException(
                status_code=400,
                detail=f"record.period 格式錯誤: {rec.period}",
            )
        try:
            sy_str, sem_str = rec.period.split("-", 1)
            school_year, semester = int(sy_str), int(sem_str)
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=400,
                detail=f"record.period 格式錯誤: {rec.period}",
            )
        sem_start, sem_end = _semester_date_range(school_year, semester)

        # 註冊費 / 雜費:走 enrollment_ratio
        if fee_type in ("registration", "miscellaneous"):
            holiday_map, makeup_map = load_day_rule_maps(session, sem_start, sem_end)
            T_total = payload.T_total_override or _count_workdays(
                sem_start, sem_end, holiday_map, makeup_map
            )
            served_end = min(payload.withdrawal_date, sem_end)
            if payload.T_served_override is not None:
                T_served = payload.T_served_override
            else:
                T_served = (
                    _count_workdays(sem_start, served_end, holiday_map, makeup_map)
                    if served_end >= sem_start
                    else 0
                )
            return calc_enrollment_refund(
                amount_due=rec.amount_due,
                T_total=T_total,
                T_served=T_served,
            )

        # 月費:走 monthly_partial
        if fee_type == "monthly":
            target_month = rec.target_month
            if not target_month:
                raise HTTPException(status_code=400, detail="月費記錄缺 target_month")
            try:
                year_str, month_str = target_month.split("-", 1)
                year, month = int(year_str), int(month_str)
            except (ValueError, AttributeError):
                raise HTTPException(
                    status_code=400,
                    detail=f"target_month 格式錯誤: {target_month}",
                )
            month_start = date(year, month, 1)
            month_end = date(year, month, monthrange(year, month)[1])
            holiday_map, makeup_map = load_day_rule_maps(
                session, month_start, month_end
            )
            work_days = _count_workdays(month_start, month_end, holiday_map, makeup_map)

            # 該學生該月所有 approved leave;判斷 advance_filed 與蒐集請假日
            leaves = (
                session.query(StudentLeaveRequest)
                .filter(
                    StudentLeaveRequest.student_id == rec.student_id,
                    StudentLeaveRequest.status == "approved",
                    StudentLeaveRequest.end_date >= month_start,
                    StudentLeaveRequest.start_date <= month_end,
                )
                .all()
            )
            advance_filed = False
            leave_dates: list[date] = []
            for lv in leaves:
                # 「事先」定義: created_at.date() < start_date
                if lv.created_at and lv.created_at.date() < lv.start_date:
                    advance_filed = True
                d = max(lv.start_date, month_start)
                end = min(lv.end_date, month_end)
                while d <= end:
                    leave_dates.append(d)
                    d = d + timedelta(days=1)

            L_consecutive = longest_consecutive_workdays(
                leave_dates, holiday_map, makeup_map
            )

            # 取 breakdown:rec.source_template_id 對應的 FeeTemplate
            breakdown = None
            if rec.source_template_id:
                tpl = (
                    session.query(FeeTemplate)
                    .filter(FeeTemplate.id == rec.source_template_id)
                    .first()
                )
                if tpl:
                    breakdown = tpl.breakdown

            return calc_monthly_refund(
                amount_due=rec.amount_due,
                breakdown=breakdown,
                L_consecutive=L_consecutive,
                work_days_in_month=work_days,
                advance_filed=advance_filed,
            )

        # custom / 其他:不提供自動建議
        return {
            "suggested_amount": 0,
            "calc_method": "manual",
            "calc_payload": {
                "fee_type": fee_type,
                "reason": "此類型無自動計算",
            },
            "warnings": ["此費用類型無自動退費規則,請手動填寫"],
        }


def _find_refund_idempotent_hit(
    session, idempotency_key: str
) -> Optional[StudentFeeRefund]:
    """查詢相同 idempotency_key 的退款紀錄（全域，不限時間視窗）。

    Why: DB 層 UniqueConstraint 已保證 idempotency_key 永久唯一。
    過去用 10 分鐘 window 過濾會造成：key 在 window 外重送 → 查不到 →
    繼續 INSERT → UNIQUE 拒絕 → 客戶端收 500（原本第一次可能已成功）。
    改為全域查詢，上下文驗證由呼叫端負責（record_id / amount 必須一致）。
    """
    return (
        session.query(StudentFeeRefund)
        .filter(StudentFeeRefund.idempotency_key == idempotency_key)
        .order_by(StudentFeeRefund.id.asc())
        .first()
    )


@router.post("/records/{record_id}/refund", status_code=201)
def refund_fee_record(
    record_id: int,
    payload: RefundRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.FEES_WRITE)),
):
    """建立退款紀錄並扣除已繳金額。

    - 退款金額必須 ≤ 當下已繳
    - 一次退款一筆，需填退款原因（稽核要求）
    - 鎖住該筆 fee record，避免與 pay_fee_record 併發衝突
    - 若帶 idempotency_key，10 分鐘視窗內同 key 視為重試，回傳原退款結果
      （避免網路重送造成重複扣款；DB UniqueConstraint 於並發時攔下第二筆）
    """
    idempotent_replay = False
    with session_scope() as session:
        # 先檢冪等：若已有紀錄，直接回放原結果，不鎖 record 也不動 amount_paid
        # 上下文必須一致（record_id / amount 相符），否則視為 key 誤用 → 409
        if payload.idempotency_key:
            existing = _find_refund_idempotent_hit(session, payload.idempotency_key)
            if existing is not None:
                if existing.record_id != record_id or existing.amount != payload.amount:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"idempotency_key 已用於 record {existing.record_id} "
                            f"（NT${existing.amount}），不可重複用於本請求"
                        ),
                    )
                rec = (
                    session.query(StudentFeeRecord)
                    .filter(StudentFeeRecord.id == record_id)
                    .first()
                )
                return {
                    "ok": True,
                    "refund_amount": existing.amount,
                    "new_amount_paid": rec.amount_paid if rec else None,
                    "status": rec.status if rec else None,
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

        paid = record.amount_paid or 0
        if paid <= 0:
            raise HTTPException(status_code=400, detail="此記錄尚未有任何繳費可退")
        if payload.amount > paid:
            raise HTTPException(
                status_code=400,
                detail=f"退款金額 NT${payload.amount} 超過已繳金額 NT${paid}",
            )

        # ── A 錢守衛 ─────────────────────────────────────────────────
        # Pydantic 已強制 reason ≥ 5 字；此處再過一層 strip 並寫回 payload
        payload.reason = require_adjustment_reason(payload.reason)
        # 累積退款簽核（最嚴格）：以同 record 過去已退 + 本次金額判斷，
        # 任一筆讓累積跨閾值即整筆需 ACTIVITY_PAYMENT_APPROVE。
        # Why: 舊版只看本次 amount，會計可拆成多筆 NT$1000 連退繞過簽核。
        prior_refunded = (
            session.query(func.coalesce(func.sum(StudentFeeRefund.amount), 0))
            .filter(StudentFeeRefund.record_id == record_id)
            .scalar()
        ) or 0
        cumulative_refund = int(prior_refunded) + int(payload.amount)
        require_finance_approve(
            cumulative_refund, current_user, action_label="學費累積退款"
        )

        operator = current_user.get("username") or current_user.get("name") or "unknown"

        refund = StudentFeeRefund(
            record_id=record.id,
            amount=payload.amount,
            reason=payload.reason,
            notes=payload.notes or "",
            refunded_by=operator,
            idempotency_key=payload.idempotency_key,
            calc_method=payload.calc_method,
            calc_payload=payload.calc_payload,
        )
        session.add(refund)

        record.amount_paid = paid - payload.amount
        # 若還有剩餘，視為 partial；若清 0 則回 unpaid
        if record.amount_paid <= 0:
            record.status = "unpaid"
        elif record.amount_paid < (record.amount_due or 0):
            record.status = "partial"
        else:
            record.status = "paid"
        record.updated_at = datetime.now()

        new_paid = record.amount_paid
        new_status = record.status
        student_name_snapshot = record.student_name

        # DB 層 UNIQUE 攔下並發同 idempotency_key 的第二筆：把它轉成 replay
        # 上下文必須一致，否則回 409 而非誤 replay
        try:
            session.flush()
        except IntegrityError as e:
            session.rollback()
            if (
                payload.idempotency_key
                and "idempotency_key" in str(getattr(e, "orig", e)).lower()
            ):
                # 另一個並發請求剛建完；重新查出來以 replay 方式回
                with session_scope() as replay_session:
                    existing = _find_refund_idempotent_hit(
                        replay_session, payload.idempotency_key
                    )
                    if existing is not None and (
                        existing.record_id != record_id
                        or existing.amount != payload.amount
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"idempotency_key 已用於 record {existing.record_id} "
                                f"（NT${existing.amount}），不可重複用於本請求"
                            ),
                        )
                    rec = (
                        replay_session.query(StudentFeeRecord)
                        .filter(StudentFeeRecord.id == record_id)
                        .first()
                    )
                    if existing is not None:
                        return {
                            "ok": True,
                            "refund_amount": existing.amount,
                            "new_amount_paid": rec.amount_paid if rec else None,
                            "status": rec.status if rec else None,
                            "idempotent_replay": True,
                        }
            raise

        # 同交易 outbox：退款的 AuditLog 必須與 StudentFeeRefund 共生死
        write_audit_in_session(
            session,
            request,
            action="UPDATE",
            entity_type="fee",
            entity_id=record_id,
            summary=(
                f"學費退款 {record.period or ''} {student_name_snapshot}: "
                f"NT${payload.amount}（{payload.reason}，by {operator}）"
            ),
            changes={
                "action": "fee_refund",
                "record_id": record_id,
                "student_id": record.student_id,
                "student_name": student_name_snapshot,
                "period": record.period,
                "paid_before": paid,
                "refund_amount": payload.amount,
                "paid_after": new_paid,
                "amount_due": record.amount_due,
                "status_after": new_status,
                "reason": payload.reason,
                "refund_id": refund.id,
                "cumulative_refund_after": cumulative_refund,
                "idempotency_key": payload.idempotency_key,
                "calc_method": payload.calc_method,
                "calc_payload": payload.calc_payload,
                "operator": operator,
            },
        )

    # session_scope commit 後失效報表快取
    _invalidate_finance_summary_cache()

    logger.warning(
        "FEE_REFUND record_id=%s student=%s operator=%s amount=%s reason=%s new_paid=%s",
        record_id,
        student_name_snapshot,
        operator,
        payload.amount,
        payload.reason,
        new_paid,
    )
    return {
        "ok": True,
        "refund_amount": payload.amount,
        "new_amount_paid": new_paid,
        "status": new_status,
        "idempotent_replay": idempotent_replay,
    }


@router.get("/records/{record_id}/refunds")
def list_fee_refunds(
    record_id: int,
    _: None = Depends(require_staff_permission(Permission.FEES_READ)),
):
    """列出某筆學費記錄的退款歷史（按時間新→舊）"""
    with session_scope() as session:
        rec = (
            session.query(StudentFeeRecord)
            .filter(StudentFeeRecord.id == record_id)
            .first()
        )
        if not rec:
            raise HTTPException(status_code=404, detail="費用記錄不存在")
        refunds = (
            session.query(StudentFeeRefund)
            .filter(StudentFeeRefund.record_id == record_id)
            .order_by(StudentFeeRefund.refunded_at.desc())
            .all()
        )
        return {
            "record_id": record_id,
            "student_name": rec.student_name,
            "total_refunded": sum(r.amount for r in refunds),
            "refunds": [
                {
                    "id": r.id,
                    "amount": r.amount,
                    "reason": r.reason,
                    "notes": r.notes or "",
                    "refunded_by": r.refunded_by,
                    "refunded_at": (
                        r.refunded_at.isoformat() if r.refunded_at else None
                    ),
                }
                for r in refunds
            ],
        }
