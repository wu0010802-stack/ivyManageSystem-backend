"""scripts/seed/payments.py — 繳費分錄／收款明細 seed（冪等）。

dev DB 已有母表：
- student_fee_records（學費應收／已繳主表，2088 筆）
- activity_registrations（才藝報名，145 筆 active）

但「收款明細」分錄表幾乎空：
- student_fee_payments（學費收款流水，append-only，1 筆）
- activity_payment_records（才藝繳費／退費明細，0 筆）

本步驟為「已繳或部分繳」的母筆補一筆對應的收款分錄，讓金流明細頁／月報
有資料可呈現。**金額與日期一律取自母表**，保證 sum(分錄) 對得起母表的已繳金額。

冪等契約：
- 學費：固定取「amount_paid > 0」中 id 最小的前 N 筆（deterministic slice，
  不依賴「是否已有分錄」做選擇），逐筆 skip-if 該 record_id 已有任何
  StudentFeePayment。重跑選到同一批、皆已有分錄 → 新增 0 筆。
  （注意：母筆若本來就有人工分錄，例如 restore254a，會被 skip，避免雙計。）
- 才藝：對全部 active 且 paid_amount > 0 的報名，逐筆 skip-if 該 registration_id
  已有任何 ActivityPaymentRecord。

跑法：
    python3 -m scripts.seed.payments

第一次印各表新增筆數，第二次應全為 0。
"""

from __future__ import annotations

import logging

from sqlalchemy import func

from scripts.seed._common import (  # noqa: F401
    session_scope,
    get_admin_user,
    rand_date_between,
    TODAY,
    YEAR_START,
)
from models.fees import StudentFeeRecord, StudentFeePayment
from models.activity import ActivityRegistration, ActivityPaymentRecord

logger = logging.getLogger("seed_payments")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

# 學費收款分錄取樣上限（deterministic：amount_paid>0 中 id 最小的前 N 筆）
FEE_PAYMENT_SAMPLE_SIZE = 300


def _seed_fee_payments(session, operator: str) -> int:
    """為已繳的學費母筆補一筆對應的 StudentFeePayment 收款分錄。

    選擇集合為「amount_paid > 0」按 id 排序的前 N 筆（與分錄是否存在無關），
    再逐筆 skip-if record_id 已有任何分錄 → 重跑選到同一批、全已有分錄 → 0 筆。
    金額 = 母筆 amount_paid，日期/方式取自母筆（payment_date / payment_method），
    保證 sum(payments) == record.amount_paid。
    """
    records = (
        session.query(StudentFeeRecord)
        .filter(StudentFeeRecord.amount_paid > 0)
        .order_by(StudentFeeRecord.id)
        .limit(FEE_PAYMENT_SAMPLE_SIZE)
        .all()
    )

    added = 0
    for rec in records:
        # 冪等：以母表 FK 判定「此母筆是否已有收款分錄」，
        # 不可改用自訂 idempotency_key 判定（會對已有人工分錄的母筆雙計）。
        exists = (
            session.query(StudentFeePayment.id)
            .filter(StudentFeePayment.record_id == rec.id)
            .first()
        )
        if exists:
            continue

        pay_date = rec.payment_date
        if pay_date is None:
            # 理論上 amount_paid>0 必有 payment_date（已驗 dev DB 為 0 筆 NULL）；
            # 防禦性回退到學年起始，仍落在界線內、不生未來。
            pay_date = YEAR_START
        # 防越界（雙保險，不生未來）。
        if pay_date > TODAY:
            pay_date = TODAY
        if pay_date < YEAR_START:
            pay_date = YEAR_START

        session.add(
            StudentFeePayment(
                record_id=rec.id,
                amount=rec.amount_paid,
                payment_date=pay_date,
                payment_method=rec.payment_method,
                notes="seed：補登既有已繳金額之收款分錄",
                operator=operator,
                # 走 record_id 判冪等，idempotency_key 僅供追溯（全域唯一）。
                idempotency_key=f"seed-fee-pay-{rec.id}",
            )
        )
        added += 1

    return added


def _seed_activity_payments(session, operator: str) -> int:
    """為已繳的才藝報名補一筆 ActivityPaymentRecord（type=payment）。

    對全部 active 且 paid_amount > 0 的報名，skip-if registration_id 已有任何
    分錄。金額 = reg.paid_amount，方式固定「現金」（POS 才藝僅收現金），
    日期取報名 created_at 當天（界線保護），保證 sum(payments) == paid_amount。
    """
    regs = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.is_active == True,  # noqa: E712
            ActivityRegistration.paid_amount > 0,
        )
        .order_by(ActivityRegistration.id)
        .all()
    )

    added = 0
    for reg in regs:
        exists = (
            session.query(ActivityPaymentRecord.id)
            .filter(ActivityPaymentRecord.registration_id == reg.id)
            .first()
        )
        if exists:
            continue

        # 日期取報名建立當天；界線保護避免越界（不生未來）。
        pay_date = reg.created_at.date() if reg.created_at else TODAY
        if pay_date > TODAY:
            pay_date = TODAY
        if pay_date < YEAR_START:
            pay_date = YEAR_START

        session.add(
            ActivityPaymentRecord(
                registration_id=reg.id,
                type="payment",
                amount=reg.paid_amount,
                payment_date=pay_date,
                payment_method="現金",
                notes="seed：補登既有已繳金額之繳費明細",
                operator=operator,
                # registration 級唯一即可；idk 全域唯一供追溯。
                idempotency_key=f"seed-act-pay-{reg.id}",
                receipt_no=f"SEED-{pay_date.strftime('%Y%m%d')}-{reg.id:08d}",
            )
        )
        added += 1

    return added


def step() -> None:
    """補登繳費分錄：student_fee_payments + activity_payment_records（冪等）。"""
    logger.info(
        "=== Step（冷門）：繳費分錄 student_fee_payments / activity_payment_records ==="
    )
    with session_scope() as session:
        admin = get_admin_user(session)
        operator = (getattr(admin, "username", None) if admin else None) or "seed"

        fee_added = _seed_fee_payments(session, operator)
        act_added = _seed_activity_payments(session, operator)

        # commit 前先 flush，讓統計讀到本次新增。
        session.flush()

        fee_total = session.query(func.count(StudentFeePayment.id)).scalar()
        act_total = session.query(func.count(ActivityPaymentRecord.id)).scalar()

        logger.info(
            "學費收款分錄 student_fee_payments：本次新增 %d 筆（表內共 %d 筆）",
            fee_added,
            fee_total,
        )
        logger.info(
            "才藝繳費明細 activity_payment_records：本次新增 %d 筆（表內共 %d 筆）",
            act_added,
            act_total,
        )


if __name__ == "__main__":
    step()
