"""
api/activity/registrations_payments.py — 才藝報名繳費／退費明細端點

含 4 個端點 + _desensitize_operator helper：
- PUT /registrations/{id}/payment       legacy 單筆 payment 直更（仍保留供舊版前端）
- GET /registrations/{id}/payments      取得繳費／退費明細（含 voided 軟刪）
- POST /registrations/{id}/payments     新增單筆繳費／退費（含 idempotency key、累積退費簽核）
- DELETE /registrations/{id}/payments/{payment_id}  軟刪 payment 紀錄（雙簽 + reason）

注意：_lock_registration 仍保留於 registrations.py（CRUD core 共用），本檔
透過 sibling import 取用，避免在 _shared.py 添加無此特殊需求的全域 helper。
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from models.database import (
    get_session,
    ActivityRegistration,
    ActivityPaymentRecord,
)
from services.activity_service import activity_service
from services.activity_payment_guards import require_approve_for_refund_diff
from services.activity_refund_query import build_refund_suggestion
from utils.errors import raise_safe_500
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.finance_guards import (
    FINANCE_APPROVAL_THRESHOLD,
    require_finance_approve,
)

from ._shared import (
    PaymentUpdate,
    AddPaymentRequest,
    VoidPaymentRequest,
    SYSTEM_RECONCILE_METHOD,
    MIN_REFUND_REASON_LENGTH,
    TAIPEI_TZ,
    _not_found,
    _calc_total_amount,
    _compute_is_paid,
    _derive_payment_status,
    _invalidate_activity_dashboard_caches,
    _invalidate_finance_summary_cache,
    _lock_registration,
    has_payment_approve,
    require_refund_reason,
    require_approve_for_large_refund,
    require_approve_for_cumulative_refund,
    _require_daily_close_unlocked,
    today_taipei,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _desensitize_operator(operator: Optional[str], viewer_has_approve: bool) -> str:
    """對 operator 欄位去敏化：非簽核權限者只看得到首字 + ***。

    Why: 員工帳號暴露給過廣的閱讀者（ACTIVITY_READ）等同於社工輔助材料；
    但對於能執行簽核的主管/老闆仍需看完整帳號以便對帳追責。
    """
    if not operator:
        return ""
    if viewer_has_approve:
        return operator
    if operator == "system":
        return "system"
    # 保留首字，其餘遮蔽（例如 "fee_admin" → "f***"）
    return operator[0] + "***"


@router.put("/registrations/{registration_id}/payment")
async def update_payment(
    registration_id: int,
    body: PaymentUpdate,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """更新付款狀態

    併發保護：鎖 reg 行，避免與 POS checkout / add_registration_payment 並發
    造成 lost update（POS 寫入的 paid_amount 可能被此處 set 覆寫）。
    """
    session = get_session()
    try:
        reg = _lock_registration(session, registration_id)
        if not reg:
            raise _not_found("報名資料")

        total_amount = _calc_total_amount(session, registration_id)
        operator = current_user.get("username", "")

        today = today_taipei()
        # 今日若已簽核，任何補齊/沖帳都會讓 snapshot 失準，先擋
        _require_daily_close_unlocked(session, today)
        if body.is_paid:
            if not reg.is_paid:
                shortfall = total_amount - (reg.paid_amount or 0)
                if shortfall > 0:
                    # ── 補齊欠費守衛 ──────────────────────────────────────
                    # 原設計直接寫「系統補齊」payment 補上欠費，無 method/原因/簽核，
                    # 會計可逐筆把欠費轉成收入流水。對齊 is_paid=False 嚴格度：
                    # 1. 必填人工 payment_method（拒絕 SYSTEM_RECONCILE_METHOD）
                    # 2. 必填 ≥5 字 payment_reason
                    # 3. shortfall 過 FINANCE_APPROVAL_THRESHOLD 需金流簽核
                    method_cleaned = (body.payment_method or "").strip()
                    if not method_cleaned:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"標記已繳費需補齊 NT${shortfall} 欠費，"
                                "請於 payment_method 填入「現金」"
                                "（目前才藝僅收現金），不接受系統補齊"
                            ),
                        )
                    if method_cleaned == SYSTEM_RECONCILE_METHOD:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"payment_method 不可填入「{SYSTEM_RECONCILE_METHOD}」，"
                                "請填寫實際收款方式以利稽核"
                            ),
                        )
                    reason_cleaned = (body.payment_reason or "").strip()
                    if len(reason_cleaned) < MIN_REFUND_REASON_LENGTH:
                        raise HTTPException(
                            status_code=400,
                            detail=(
                                f"標記已繳費需補齊 NT${shortfall}，"
                                f"請於 payment_reason 填寫原因（≥ {MIN_REFUND_REASON_LENGTH} 字）"
                            ),
                        )
                    require_finance_approve(
                        shortfall,
                        current_user,
                        threshold=FINANCE_APPROVAL_THRESHOLD,
                        action_label="補齊欠費金額",
                    )
                    rec = ActivityPaymentRecord(
                        registration_id=registration_id,
                        type="payment",
                        amount=shortfall,
                        payment_date=today,
                        payment_method=method_cleaned,
                        notes=f"（標記已繳費補齊）方式：{method_cleaned}；原因：{reason_cleaned}",
                        operator=operator,
                    )
                    session.add(rec)
                    reg.paid_amount = total_amount
                reg.is_paid = _compute_is_paid(reg.paid_amount or 0, total_amount)
        else:
            # is_paid=False：一刀切全額沖帳會誤殺部分繳費者，收緊為「必須帶
            # confirm_refund_amount == current_paid 且 refund_reason ≥ 5 字」。
            current_paid = reg.paid_amount or 0
            if body.confirm_refund_amount is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"標記未繳費將沖帳全額已繳 NT${current_paid}，"
                        "請於 confirm_refund_amount 明確填寫同金額以二次確認"
                    ),
                )
            if body.confirm_refund_amount != current_paid:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"confirm_refund_amount NT${body.confirm_refund_amount} "
                        f"與當前已繳 NT${current_paid} 不符，請重新確認"
                    ),
                )
            reason_cleaned = require_refund_reason(body.refund_reason)
            # 大額沖帳需簽核權限（以「該 reg 累積退費 + 本次」判斷，封拆單繞過）
            require_approve_for_cumulative_refund(
                session,
                registration_id,
                current_paid,
                current_user,
                label="標記未繳費自動沖帳累積退費總額",
            )
            if current_paid > 0:
                rec = ActivityPaymentRecord(
                    registration_id=registration_id,
                    type="refund",
                    amount=current_paid,
                    payment_date=today,
                    payment_method=SYSTEM_RECONCILE_METHOD,
                    notes=f"（標記未繳費自動沖帳）原因：{reason_cleaned}",
                    operator=operator,
                )
                session.add(rec)
            reg.paid_amount = 0
            reg.is_paid = False

        status_str = "已繳費" if body.is_paid else "未繳費"
        activity_service.log_change(
            session,
            registration_id,
            reg.student_name,
            "更新付款狀態",
            f"付款狀態更新為：{status_str}",
            operator,
        )
        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        _invalidate_finance_summary_cache()
        request.state.audit_summary = f"更新繳費狀態：{reg.student_name} → {status_str}"
        request.state.audit_changes = {
            "student_name": reg.student_name,
            "new_is_paid": bool(body.is_paid),
            "paid_amount_after": reg.paid_amount,
            "total_amount": total_amount,
        }
        return {"message": f"更新成功，狀態為：{status_str}"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/registrations/{registration_id}/payments")
async def get_registration_payments(
    registration_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得報名的繳費／退費明細記錄（含 voided 軟刪紀錄，標示 is_voided）"""
    session = get_session()
    try:
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == registration_id,
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        if not reg:
            raise _not_found("報名資料")

        records = (
            session.query(ActivityPaymentRecord)
            .filter(ActivityPaymentRecord.registration_id == registration_id)
            .order_by(ActivityPaymentRecord.created_at.asc())
            .all()
        )
        total_amount = _calc_total_amount(session, registration_id)
        paid_amount = reg.paid_amount or 0
        viewer_has_approve = has_payment_approve(current_user)
        return {
            "total_amount": total_amount,
            "paid_amount": paid_amount,
            "payment_status": _derive_payment_status(paid_amount, total_amount),
            "records": [
                {
                    "id": r.id,
                    "type": r.type,
                    "amount": r.amount,
                    "payment_date": (
                        r.payment_date.isoformat() if r.payment_date else None
                    ),
                    "payment_method": r.payment_method or "",
                    "notes": r.notes or "",
                    "operator": _desensitize_operator(r.operator, viewer_has_approve),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "is_voided": r.voided_at is not None,
                    "voided_at": (r.voided_at.isoformat() if r.voided_at else None),
                    "voided_by": _desensitize_operator(r.voided_by, viewer_has_approve),
                    "void_reason": r.void_reason or "",
                }
                for r in records
            ],
        }
    finally:
        session.close()


_IDEMPOTENCY_WINDOW_SECONDS = 600


@router.post("/registrations/{registration_id}/payments", status_code=201)
async def add_registration_payment(
    registration_id: int,
    body: AddPaymentRequest,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """新增繳費或退費記錄

    冪等語意（2026-04-24 修正）：
    idempotency_key 於 DB 層永久全域唯一。同 key 必須對應同一 registration、
    同一 type、同一 amount；若上下文不符視為 key 誤用，回 409 避免錯帳到
    其他 registration（原本的 10 分鐘 window 過期後再重送會爆 500）。
    """
    session = get_session()
    try:
        # ── 冪等性重送檢查（先於任何寫入） ────────────────────────
        # 與 pos._find_idempotent_hit 對齊：排除 voided 紀錄。否則「key 命中但
        # 全 voided」會被當作合法 replay 回 200，但 DB 並無新紀錄、paid_amount
        # 反映 void 後（=0），員工以為已收實際永久漏收。Refs: 邏輯漏洞 audit
        # 2026-05-07 P0 (#7)。
        if body.idempotency_key:
            from .pos import _find_idempotent_hit, _has_any_record_for_key

            hit = _find_idempotent_hit(session, body.idempotency_key)
            if hit is None and _has_any_record_for_key(session, body.idempotency_key):
                # key 已用於 voided 紀錄；不可重複 replay 也不可作為新交易 key
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "idempotency_key 對應的紀錄已被作廢；請改用新 key "
                        "重新建立繳費/退費記錄"
                    ),
                )
            if hit is not None:
                # 上下文一致才 replay；不一致視為 key 誤用
                if (
                    hit.registration_id != registration_id
                    or hit.type != body.type
                    or hit.amount != body.amount
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"idempotency_key 已用於 registration {hit.registration_id} "
                            f"（{hit.type} NT${hit.amount}），不可重複用於本請求"
                        ),
                    )
                reg_hit = (
                    session.query(ActivityRegistration)
                    .filter(ActivityRegistration.id == hit.registration_id)
                    .first()
                )
                total_amount = _calc_total_amount(session, hit.registration_id)
                paid = (reg_hit.paid_amount if reg_hit else 0) or 0
                type_label = "繳費" if hit.type == "payment" else "退費"
                logger.info(
                    "add_registration_payment idempotent replay: key=%s reg=%s",
                    body.idempotency_key,
                    hit.registration_id,
                )
                return {
                    "message": f"{type_label}記錄新增成功",
                    "paid_amount": paid,
                    "payment_status": _derive_payment_status(paid, total_amount),
                }

        # 已簽核日守衛（payment_date 落在 daily-close 之日則拒絕）
        _require_daily_close_unlocked(session, body.payment_date)

        # ── 退費 reason 必填（schema 已強制；此處 cleaned 並覆寫）────
        # Pydantic 已在 schema 層強制 type=refund 時 notes ≥ MIN_REFUND_REASON_LENGTH；
        # 此處再檢一次（防 schema 日後被放寬）並處理 cleaned notes
        if body.type == "refund":
            cleaned_reason = require_refund_reason(body.notes)
            body.notes = cleaned_reason

        # 行級鎖住該 registration，防併發繳/退費 lost update
        reg = _lock_registration(session, registration_id)
        if not reg:
            raise _not_found("報名資料")

        # ── 累積退費簽核（必須在 _lock_registration 之後）─────────────
        # 鎖之後才查 prior_refunded，確保兩個併發小額退費不會各自看到相同舊累積值
        # 而各自通過簽核門檻；以同 registration 過去未作廢的退費 + 本次金額判斷，
        # 任一筆讓累積跨閾值即整筆需 ACTIVITY_PAYMENT_APPROVE。
        # Why: 舊版在 lock 前就算 prior_refunded，存在 race window；舊版亦有「只看本次
        # body.amount」的拆單問題。本次累積簽核同時封死兩條繞過路徑。
        if body.type == "refund":
            prior_refunded = (
                session.query(func.coalesce(func.sum(ActivityPaymentRecord.amount), 0))
                .filter(
                    ActivityPaymentRecord.registration_id == registration_id,
                    ActivityPaymentRecord.type == "refund",
                    ActivityPaymentRecord.voided_at.is_(None),
                )
                .scalar()
            ) or 0
            cumulative_refund = int(prior_refunded) + int(body.amount)
            require_approve_for_large_refund(
                cumulative_refund, current_user, label="活動累積退費總額"
            )

        # ── 第三道：實退 vs 建議值偏離簽核 (spec §8.2) ───────────────
        if body.type == "refund":
            suggestion = build_refund_suggestion(session, registration_id)
            suggested_total = suggestion["total_suggested_amount"]
            diff = abs(int(body.amount) - suggested_total)
            require_approve_for_refund_diff(
                diff=diff,
                current_user=current_user,
                suggested_total=suggested_total,
                actual_total=int(body.amount),
            )
            _refund_audit_context = {
                "suggested_total": suggested_total,
                "actual_total": int(body.amount),
                "diff": diff,
                "suggestion_details": [suggestion],
            }
        else:
            _refund_audit_context = {}

        operator = current_user.get("username", "")

        if body.type == "refund" and body.amount > (reg.paid_amount or 0):
            raise HTTPException(
                status_code=400,
                detail=f"退費金額 NT${body.amount} 超過已繳金額 NT${reg.paid_amount or 0}",
            )

        # 空報名守衛：與 POS checkout 對齊，避免對無應繳的殼報名寫入付款，產生孤兒金額。
        # 僅擋「空報名收款」，不擋超收（overpaid 是系統支援的四態之一，admin 可能需要手動處理）
        if body.type == "payment":
            current_total = _calc_total_amount(session, registration_id)
            if current_total <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"報名 {registration_id}（{reg.student_name}）無應繳金額，"
                        f"無法新增繳費記錄"
                    ),
                )

        rec = ActivityPaymentRecord(
            registration_id=registration_id,
            type=body.type,
            amount=body.amount,
            payment_date=body.payment_date,
            payment_method=body.payment_method,
            notes=body.notes,
            operator=operator,
            idempotency_key=body.idempotency_key,
        )
        session.add(rec)

        if body.type == "payment":
            reg.paid_amount = (reg.paid_amount or 0) + body.amount
        else:
            # max(0, ...) 防禦：即使驗證通過到執行之間狀態被搶改，也不會變負。
            reg.paid_amount = max(0, (reg.paid_amount or 0) - body.amount)

        total_amount = _calc_total_amount(session, registration_id)
        reg.is_paid = _compute_is_paid(reg.paid_amount or 0, total_amount)

        type_label = "繳費" if body.type == "payment" else "退費"
        activity_service.log_change(
            session,
            registration_id,
            reg.student_name,
            f"新增{type_label}記錄",
            f"{type_label} NT${body.amount}，繳費方式：{body.payment_method}",
            operator,
        )
        try:
            session.commit()
        except IntegrityError as e:
            # DB 層 UNIQUE 攔下並發同 idempotency_key 的第二筆：轉為 idempotent replay
            # 重要：必須驗證 (registration_id, type, amount) 一致，否則視為 key 誤用
            session.rollback()
            if body.idempotency_key and "idempotency_key" in str(e.orig).lower():
                hit = (
                    session.query(ActivityPaymentRecord)
                    .filter(
                        ActivityPaymentRecord.idempotency_key == body.idempotency_key
                    )
                    .order_by(ActivityPaymentRecord.id.asc())
                    .first()
                )
                if hit is not None:
                    if (
                        hit.registration_id != registration_id
                        or hit.type != body.type
                        or hit.amount != body.amount
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"idempotency_key 已用於 registration "
                                f"{hit.registration_id}（{hit.type} NT${hit.amount}），"
                                f"不可重複用於本請求"
                            ),
                        )
                    reg_hit = (
                        session.query(ActivityRegistration)
                        .filter(ActivityRegistration.id == hit.registration_id)
                        .first()
                    )
                    total_hit = _calc_total_amount(session, hit.registration_id)
                    paid_hit = (reg_hit.paid_amount if reg_hit else 0) or 0
                    type_label_hit = "繳費" if hit.type == "payment" else "退費"
                    logger.info(
                        "add_registration_payment idempotent replay via UNIQUE: key=%s reg=%s",
                        body.idempotency_key,
                        hit.registration_id,
                    )
                    return {
                        "message": f"{type_label_hit}記錄新增成功",
                        "paid_amount": paid_hit,
                        "payment_status": _derive_payment_status(paid_hit, total_hit),
                    }
            raise
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        _invalidate_finance_summary_cache()
        request.state.audit_summary = (
            f"新增{type_label}記錄：{reg.student_name} NT${body.amount}"
        )
        request.state.audit_changes = {
            "student_name": reg.student_name,
            "type": body.type,
            "amount": body.amount,
            "payment_method": body.payment_method,
            "payment_date": body.payment_date.isoformat(),
            "paid_amount_after": reg.paid_amount,
        }
        # spec §12：退費路徑擴充 audit_changes 含 calculator 建議值反差
        if body.type == "refund" and _refund_audit_context:
            request.state.audit_changes.update(
                {
                    "refund_suggested_total": _refund_audit_context["suggested_total"],
                    "refund_actual_total": _refund_audit_context["actual_total"],
                    "refund_diff": _refund_audit_context["diff"],
                    "refund_suggestion_per_reg": [
                        {
                            "registration_id": sd["registration_id"],
                            "total_suggested": sd["total_suggested_amount"],
                            "items": [
                                {
                                    "type": it["type"],
                                    "target_id": it["target_id"],
                                    "suggested": it["suggested_amount"],
                                    "calc_method": it["calc_method"],
                                }
                                for it in sd["items"]
                            ],
                        }
                        for sd in _refund_audit_context["suggestion_details"]
                    ],
                }
            )
        return {
            "message": f"{type_label}記錄新增成功",
            "paid_amount": reg.paid_amount,
            "payment_status": _derive_payment_status(reg.paid_amount, total_amount),
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/registrations/{registration_id}/payments/{payment_id}")
async def delete_registration_payment(
    registration_id: int,
    payment_id: int,
    request: Request,
    body: VoidPaymentRequest,
    current_user: dict = Depends(
        require_staff_permission(Permission.ACTIVITY_PAYMENT_APPROVE)
    ),
):
    """軟刪除（void）繳費記錄：原紀錄保留於 DB，設 voided_at/by/reason 並重算已繳金額。

    Why: 員工可能濫用「POS 收現金 → DELETE payment → paid_amount 歸零 → 私吞」；
    改軟刪後：
      1. 原 payment row 永不消失，稽核可追溯完整金流
      2. 需 ACTIVITY_PAYMENT_APPROVE（簽核權限）且強制填寫原因（≥5 字）
      3. paid_amount / daily snapshot 重算時以 voided_at IS NULL 為前提排除

    併發保護：鎖 reg 行，避免 GROUP BY 重算與 POS checkout 並發時，
    POS 的新付款在本端 commit 時被 paid_amount = 舊 sum 的 UPDATE 覆蓋（lost update）。
    """
    session = get_session()
    try:
        reg = _lock_registration(session, registration_id)
        if not reg:
            raise _not_found("報名資料")

        payment = (
            session.query(ActivityPaymentRecord)
            .filter(
                ActivityPaymentRecord.id == payment_id,
                ActivityPaymentRecord.registration_id == registration_id,
            )
            .first()
        )
        if not payment:
            raise _not_found("繳費記錄")

        # 已軟刪的紀錄不可重複 void，避免操作紀錄被洗成多次 void
        if payment.voided_at is not None:
            raise HTTPException(
                status_code=409,
                detail="此繳費記錄已於稍早被軟刪，不可重複操作",
            )

        # 若被刪除的付款日期已被日結簽核，拒絕刪除以免 snapshot 與 DB 失準
        _require_daily_close_unlocked(session, payment.payment_date)

        operator = current_user.get("username", "")
        now = datetime.now(TAIPEI_TZ).replace(tzinfo=None)
        payment.voided_at = now
        payment.voided_by = operator
        payment.void_reason = body.reason

        deleted_snapshot = {
            "type": payment.type,
            "amount": payment.amount,
            "payment_date": (
                payment.payment_date.isoformat() if payment.payment_date else None
            ),
        }

        session.flush()

        # 重算 paid_amount：以 voided_at IS NULL 為前提，排除軟刪紀錄
        totals = (
            session.query(
                ActivityPaymentRecord.type, func.sum(ActivityPaymentRecord.amount)
            )
            .filter(
                ActivityPaymentRecord.registration_id == registration_id,
                ActivityPaymentRecord.voided_at.is_(None),
            )
            .group_by(ActivityPaymentRecord.type)
            .all()
        )
        amount_map = {t: s for t, s in totals}
        new_paid = (amount_map.get("payment") or 0) - (amount_map.get("refund") or 0)
        reg.paid_amount = max(0, new_paid)

        total_amount = _calc_total_amount(session, registration_id)
        reg.is_paid = _compute_is_paid(reg.paid_amount or 0, total_amount)

        activity_service.log_change(
            session,
            registration_id,
            reg.student_name,
            "軟刪除繳費記錄",
            (
                f"void payment_id={payment_id}（{deleted_snapshot['type']} NT${deleted_snapshot['amount']}），"
                f"原因：{body.reason}，重新計算已繳 NT${reg.paid_amount}"
            ),
            operator,
        )
        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        _invalidate_finance_summary_cache()
        # URL 尾段為 payment_id，middleware 預設會抓成 entity_id；覆寫為 registration_id
        # 才能讓「該筆報名的所有稽核事件」查詢命中此筆。
        request.state.audit_entity_id = str(registration_id)
        request.state.audit_summary = (
            f"軟刪繳費記錄：{reg.student_name} payment_id={payment_id} "
            f"NT${deleted_snapshot['amount']}（{deleted_snapshot['type']}）原因：{body.reason}"
        )
        request.state.audit_changes = {
            "student_name": reg.student_name,
            "voided_payment_id": payment_id,
            "voided_type": deleted_snapshot["type"],
            "voided_amount": deleted_snapshot["amount"],
            "voided_payment_date": deleted_snapshot["payment_date"],
            "void_reason": body.reason,
            "paid_amount_after": reg.paid_amount,
        }
        return {
            "message": "記錄已軟刪（原紀錄保留供稽核）",
            "paid_amount": reg.paid_amount,
            "payment_status": _derive_payment_status(reg.paid_amount, total_amount),
            "voided_at": now.isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
