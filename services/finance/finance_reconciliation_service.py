"""
services/finance_reconciliation_service.py — 才藝 POS paid_amount 對帳偵測。

業務語意（spec H4）：
ActivityRegistration.paid_amount 為物理欄位（POS checkout / void / 補齊等
路徑各自更新），但真相應等於該 registration 所有非 voided 的
ActivityPaymentRecord 淨額（payment - refund）。歷史匯入、bug 或意外路徑
可能造成兩者偏差 → semester-reconciliation 會以 offline_paid_amount > 0
顯示，但「沒人會每天看」。本模組提供純函式偵測 helper，由 daily cron 呼叫
並推播 LINE 警示給老闆。
"""

import logging
from dataclasses import dataclass
from typing import List

from sqlalchemy import case, func

from models.database import ActivityPaymentRecord, ActivityRegistration

logger = logging.getLogger(__name__)


@dataclass
class PaidAmountMismatch:
    """單筆對帳異常：paid_amount 與 payment_records 淨額不符。"""

    registration_id: int
    student_name: str
    class_name: str
    db_paid_amount: int
    records_net: int  # SUM(payment) - SUM(refund)，排除 voided
    drift: int  # db_paid_amount - records_net；正值=DB 多算，負值=DB 少算


def detect_paid_amount_mismatches(session) -> List[PaidAmountMismatch]:
    """掃描所有 active registrations，比對 paid_amount vs payment_records 淨額。

    Why: paid_amount 是物理欄位需手動同步；voided 後若忘了重算、或歷史匯入
    把 paid_amount 直接寫入但無對應 records，會造成 drift。本 helper 用 SQL
    aggregate 一次撈出所有不一致紀錄，供告警通道顯示。

    Returns: 不一致清單；list 為空表示帳一致。

    Performance: 單一 GROUP BY 聚合，對 active registrations 過濾；
    幼稚園規模（< 1000 active regs）執行 < 1s。
    """
    payment_sum = func.sum(
        case(
            (ActivityPaymentRecord.type == "payment", ActivityPaymentRecord.amount),
            else_=0,
        )
    )
    refund_sum = func.sum(
        case(
            (ActivityPaymentRecord.type == "refund", ActivityPaymentRecord.amount),
            else_=0,
        )
    )

    rows = (
        session.query(
            ActivityRegistration.id,
            ActivityRegistration.student_name,
            ActivityRegistration.class_name,
            ActivityRegistration.paid_amount,
            payment_sum.label("p_sum"),
            refund_sum.label("r_sum"),
        )
        .outerjoin(
            ActivityPaymentRecord,
            (ActivityPaymentRecord.registration_id == ActivityRegistration.id)
            & (ActivityPaymentRecord.voided_at.is_(None)),
        )
        .filter(ActivityRegistration.is_active.is_(True))
        .group_by(
            ActivityRegistration.id,
            ActivityRegistration.student_name,
            ActivityRegistration.class_name,
            ActivityRegistration.paid_amount,
        )
        .all()
    )

    mismatches: List[PaidAmountMismatch] = []
    for r in rows:
        db_paid = int(r.paid_amount or 0)
        records_net = int((r.p_sum or 0) - (r.r_sum or 0))
        drift = db_paid - records_net
        if drift != 0:
            mismatches.append(
                PaidAmountMismatch(
                    registration_id=r.id,
                    student_name=r.student_name,
                    class_name=r.class_name or "",
                    db_paid_amount=db_paid,
                    records_net=records_net,
                    drift=drift,
                )
            )
    return mismatches


def format_mismatches_for_line(
    mismatches: List[PaidAmountMismatch], date_iso: str
) -> str:
    """把 mismatch 清單格式化為 LINE 推播訊息（純函式，便於測試）。

    顯示前 10 筆細節，超過時附「...其餘 N 筆」提示。
    """
    if not mismatches:
        return ""
    count = len(mismatches)
    total_drift = sum(m.drift for m in mismatches)
    lines = [
        f"⚠️ POS 對帳異常通知（{date_iso}）",
        f"發現 {count} 筆 paid_amount 與 payment_records 不一致：",
        "",
    ]
    for m in mismatches[:10]:
        lines.append(
            f"• #{m.registration_id} {m.student_name}（{m.class_name or '—'}）"
        )
        lines.append(
            f"  DB paid={m.db_paid_amount} vs 紀錄淨額={m.records_net}"
            f"（差額 {m.drift:+d}）"
        )
    if count > 10:
        lines.append(f"...（其餘 {count - 10} 筆請至後台「學期對帳」查看）")
    lines.append("")
    lines.append(f"差額合計：{total_drift:+d}")
    return "\n".join(lines)
