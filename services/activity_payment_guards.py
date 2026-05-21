"""services/activity_payment_guards.py — 才藝金流簽核守衛（F2 第五階段抽出）。

從 api/activity/_shared.py 抽出 5 個權限/原因/閾值守衛 helper：
- has_payment_approve — 檢查 caller 是否具 ACTIVITY_PAYMENT_APPROVE 權限
- require_refund_reason — 退費 notes ≥ 15 字
- require_approve_for_high_price — 單品價超 30K 需簽核
- require_approve_for_large_refund — 單筆退費超 1000 需簽核
- require_approve_for_cumulative_refund — 累積退費跨閾值需簽核（拆單繞過防護）

api/activity/_shared.py 保留 re-export 維持 6+ 個 router 既有 import surface。
"""

from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func

from models.database import ActivityPaymentRecord
from utils.activity_constants import (
    ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD,
    MIN_REFUND_REASON_LENGTH,
    REFUND_APPROVAL_THRESHOLD,
)
from utils.permissions import Permission, has_permission


def has_payment_approve(current_user: dict) -> bool:
    """檢查使用者是否具備 ACTIVITY_PAYMENT_APPROVE 權限（老闆/高階簽核）。

    用於：大額退費審批、DELETE payment 軟刪審批。避免只有 ACTIVITY_WRITE 的一線員工
    直接執行敏感金流動作。
    """
    perms = current_user.get("permission_names")
    return has_permission(perms, Permission.ACTIVITY_PAYMENT_APPROVE)


def require_refund_reason(notes: Optional[str]) -> str:
    """驗證退費 notes（原因）必填且 ≥ MIN_REFUND_REASON_LENGTH 字。

    供 POS refund / add_registration_payment(type=refund) 共用。
    """
    cleaned = (notes or "").strip()
    if len(cleaned) < MIN_REFUND_REASON_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"退費必須填寫原因（至少 {MIN_REFUND_REASON_LENGTH} 個字）",
        )
    return cleaned


def require_approve_for_high_price(
    amount: int, current_user: dict, *, label: str = "單品價格"
) -> None:
    """若單品價格超過 ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD，檢查 ACTIVITY_PAYMENT_APPROVE。

    用於 Course/Supply create/update：避免 ACTIVITY_WRITE 一線權限可任意設定極端
    高價，搭配補齊收入路徑放大舞弊金額。
    """
    if amount > ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD and not has_payment_approve(
        current_user
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                f"{label} NT${amount:,} 超過 NT${ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD:,} 審批閾值，"
                f"需由具備『才藝課收款簽核』權限者執行"
            ),
        )


def require_approve_for_large_refund(
    amount: int, current_user: dict, *, label: str = "單筆退費金額"
) -> None:
    """若退費金額超過 REFUND_APPROVAL_THRESHOLD，檢查 ACTIVITY_PAYMENT_APPROVE 權限。

    `amount` 可為單筆金額或「累積後總額」；`label` 控制錯誤訊息語意，
    呼叫端傳累積值時請覆寫為「累積退費總額」等清楚字樣。
    不足即 403。供 POS refund / add_registration_payment(type=refund) 共用。
    """
    if amount > REFUND_APPROVAL_THRESHOLD and not has_payment_approve(current_user):
        raise HTTPException(
            status_code=403,
            detail=(
                f"{label} NT${amount} 超過 NT${REFUND_APPROVAL_THRESHOLD} 審批閾值，"
                f"需由具備『才藝課收款簽核』權限者執行"
            ),
        )


def require_approve_for_cumulative_refund(
    session,
    registration_id: int,
    this_refund_amount: int,
    current_user: dict,
    *,
    label: str,
) -> None:
    """以「該 reg 已存在 voided=NULL 的 refund 累積 + 本次」判斷是否跨閾值。

    Why: 與 add_registration_payment / pos.refund 既有累積判斷對齊。
    退課自動沖帳、刪除報名自動沖帳、標記未繳全額沖帳這三條 legacy 路徑只用
    「本次金額」過 require_approve_for_large_refund，可拆單跨閾值繞過簽核
    （reg 已退 NT$600 → 再退 NT$900 兩筆都 < NT$1000，但累積 NT$1500 應簽核）。

    Refs: 邏輯漏洞 audit 2026-05-07 P0 (#8)。
    """
    prior = (
        session.query(func.coalesce(func.sum(ActivityPaymentRecord.amount), 0))
        .filter(
            ActivityPaymentRecord.registration_id == registration_id,
            ActivityPaymentRecord.type == "refund",
            ActivityPaymentRecord.voided_at.is_(None),
        )
        .scalar()
    ) or 0
    cumulative = int(prior) + int(this_refund_amount)
    require_approve_for_large_refund(cumulative, current_user, label=label)
