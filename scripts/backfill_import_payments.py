"""backfill_import_payments.py

一次性腳本：為「reg.paid_amount > 0 但沒有對應 ActivityPaymentRecord」的歷史
報名補上一筆 payment_record，金額 = reg.paid_amount − 現有 POS 紀錄淨額。
補齊後該金額就能進入 POS 日結對帳 / 簽核流程。

執行方式（在 ivy-backend/ 目錄下）：

  # 1) 先 dry-run 看有哪些要補、金額多少
  python scripts/backfill_import_payments.py --payment-date 2026-04-20 --dry-run

  # 2) 確認後執行（必須指定一個未簽核的 payment_date）
  python scripts/backfill_import_payments.py --payment-date 2026-04-20 --execute

補上的 record:
  - type='payment'
  - amount=差額
  - payment_method='匯入'
  - notes='[IMPORT] 歷史匯入補齊'
  - operator='system'
  - payment_date = 你指定的日期（必須是未日結簽核的日期）

跑完後到 POS 簽核頁把該 payment_date 日結簽核，這些金額就會在「學期對帳」
從「非 POS 已繳」移到「已簽核金額」。
"""

import argparse
import logging
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func

from models.database import (
    ActivityPaymentRecord,
    ActivityPosDailyClose,
    ActivityRegistration,
    session_scope,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

IMPORT_METHOD = "匯入"
IMPORT_NOTES = "[IMPORT] 歷史匯入補齊"


def _compute_diffs(session):
    """回傳 [(reg, diff), ...]，diff = reg.paid_amount − (sum payment − sum refund)。

    只取 is_active=True 且 paid_amount > 0 的 reg；diff > 0 才納入。
    """
    regs = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.is_active.is_(True),
            ActivityRegistration.paid_amount > 0,
        )
        .all()
    )
    if not regs:
        return []
    reg_ids = [r.id for r in regs]

    # 批次算每個 reg 的 net payment
    rows = (
        session.query(
            ActivityPaymentRecord.registration_id,
            ActivityPaymentRecord.type,
            func.coalesce(func.sum(ActivityPaymentRecord.amount), 0),
        )
        .filter(ActivityPaymentRecord.registration_id.in_(reg_ids))
        .group_by(
            ActivityPaymentRecord.registration_id,
            ActivityPaymentRecord.type,
        )
        .all()
    )
    net_map: dict = {}
    for rid, ttype, amt in rows:
        cur = net_map.setdefault(rid, 0)
        if ttype == "refund":
            net_map[rid] = cur - int(amt or 0)
        else:
            net_map[rid] = cur + int(amt or 0)

    diffs = []
    for reg in regs:
        tracked = net_map.get(reg.id, 0)
        diff = (reg.paid_amount or 0) - tracked
        if diff > 0:
            # 只帶基本欄位出去，避免跨 session 存取 ORM 物件觸發 DetachedInstanceError
            diffs.append(
                {
                    "reg_id": reg.id,
                    "student_name": reg.student_name,
                    "class_name": reg.class_name or "",
                    "diff": int(diff),
                }
            )
    return diffs


def _ensure_date_unlocked(session, target: date) -> None:
    closed = (
        session.query(ActivityPosDailyClose)
        .filter(ActivityPosDailyClose.close_date == target)
        .first()
    )
    if closed is not None:
        raise SystemExit(
            f"錯誤：{target.isoformat()} 已完成日結簽核，無法回補。請換一個未簽核的日期，"
            f"或先解鎖 {target.isoformat()} 的日結。"
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--payment-date",
        required=True,
        help="要寫入的 payment_date（YYYY-MM-DD）；必須是未日結的日期",
    )
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dry-run", action="store_true", help="只列出會補的筆數與金額，不寫入"
    )
    group.add_argument("--execute", action="store_true", help="實際寫入 DB")
    args = ap.parse_args()

    try:
        pd = date.fromisoformat(args.payment_date)
    except ValueError:
        raise SystemExit("--payment-date 格式錯誤，應為 YYYY-MM-DD")
    if pd > date.today():
        raise SystemExit("--payment-date 不可為未來日期")

    with session_scope() as session:
        _ensure_date_unlocked(session, pd)
        diffs = _compute_diffs(session)

    if not diffs:
        logger.info("沒有需要補齊的 registration。")
        return

    total_amount = sum(d["diff"] for d in diffs)
    logger.info(
        "待補齊：%d 筆 registration，總金額 NT$%s，payment_date=%s",
        len(diffs),
        f"{total_amount:,}",
        pd.isoformat(),
    )
    for row in diffs[:20]:
        logger.info(
            "  reg %s %s（%s） +%s",
            row["reg_id"],
            row["student_name"],
            row["class_name"] or "—",
            row["diff"],
        )
    if len(diffs) > 20:
        logger.info("  …（還有 %d 筆未列出）", len(diffs) - 20)

    if args.dry_run:
        logger.info("Dry-run 結束，沒有寫入。加 --execute 正式執行。")
        return

    # 正式寫入
    with session_scope() as session:
        _ensure_date_unlocked(session, pd)
        diffs = _compute_diffs(session)  # 再算一次以防 race
        inserted = 0
        for row in diffs:
            rec = ActivityPaymentRecord(
                registration_id=row["reg_id"],
                type="payment",
                amount=row["diff"],
                payment_date=pd,
                payment_method=IMPORT_METHOD,
                notes=IMPORT_NOTES,
                operator="system",
                created_at=datetime.now(),
            )
            session.add(rec)
            inserted += 1
        session.commit()
    logger.info("完成，已寫入 %d 筆 payment_record。", inserted)
    logger.info(
        "下一步：到 POS 收款簽核頁將 %s 日結簽核，這些金額會納入『已簽核金額』。",
        pd.isoformat(),
    )


if __name__ == "__main__":
    main()
