"""services/activity_daily_snapshot.py — POS 日結與每日快照（F2 第八階段抽出）。

從 api/activity/_shared.py 抽出 3 個 helper：
- _is_daily_closed — 該日是否已完成 POS 日結簽核
- _require_daily_close_unlocked — 拒絕寫入已簽核日的交易
- compute_daily_snapshot — 某日 POS 流水即時快照（payment/refund/net/by_method）

api/activity/_shared.py 保留 re-export 維持既有 import surface（pos.py / public.py /
registrations.py / activity_student_sync.py 等多模組共用）。

設計：本檔不依賴 _shared.py，可被 services/activity_student_sync.py 直接 top-level import，
解除前一階段函式內 lazy import 的循環依賴。
"""

from collections import defaultdict
from datetime import date

from fastapi import HTTPException
from sqlalchemy import func

from models.database import ActivityPaymentRecord, ActivityPosDailyClose
from utils.advisory_lock import acquire_activity_daily_close_lock


def _is_daily_closed(session, target_date: date) -> bool:
    """判斷 target_date 是否已完成 POS 日結簽核。供 service / router 共用。"""
    if target_date is None:
        return False
    closed = (
        session.query(ActivityPosDailyClose.close_date)
        .filter(ActivityPosDailyClose.close_date == target_date)
        .first()
    )
    return closed is not None


def _require_daily_close_unlocked(session, target_date: date) -> None:
    """拒絕寫入 payment_date 落在已 daily-close 的紀錄。

    Why: payment_date 允許回補 30 天，但若該日已被老闆簽核，snapshot 已凍結。
    此時新增交易會讓 DB 實際值與 snapshot 永久失準（reconciliation 永遠用 snapshot）。

    M2 鎖協議：先取 per-date advisory lock（PG xact lock，commit/rollback 釋放）
    再讀 close 表，與簽核端 approve_daily_close 共用同一把鎖——否則「守衛讀到
    未簽核 ↔ 老闆同時簽核（snapshot 看不到未 commit 的寫入）」的 check-then-act
    race 會讓兩邊都成功、凍結 snapshot 永久漏單。鎖持有到 transaction 結束，
    故守衛之後的本筆寫入也在鎖下完成。SQLite 測試環境降級 no-op。
    """
    if target_date is not None:
        acquire_activity_daily_close_lock(session, target_date)
    if _is_daily_closed(session, target_date):
        raise HTTPException(
            status_code=400,
            detail=(
                f"日期 {target_date.isoformat()} 已完成日結簽核，"
                f"無法再新增/修改該日交易。請先解鎖日結後再操作。"
            ),
        )


def compute_daily_snapshot(session, target_date: date) -> dict:
    """某日 POS 流水即時快照：payment_total / refund_total / net_total / transaction_count / by_method。

    供 POS daily-summary 端點與日結簽核共用，避免邏輯雙寫。
    by_method 為 dict：員工輸入只可能是「現金」（POS schema 收口）；
    系統內部沖帳會出現「系統補齊」；method 為 NULL 者歸類為「未指定」（歷史資料）。

    Voided 紀錄（軟刪）一律排除，避免讓老闆簽核的總額包含已被作廢的交易。
    """
    rows = (
        session.query(
            ActivityPaymentRecord.type,
            ActivityPaymentRecord.payment_method,
            func.count(ActivityPaymentRecord.id),
            func.coalesce(func.sum(ActivityPaymentRecord.amount), 0),
        )
        .filter(
            ActivityPaymentRecord.payment_date == target_date,
            ActivityPaymentRecord.voided_at.is_(None),
        )
        .group_by(
            ActivityPaymentRecord.type,
            ActivityPaymentRecord.payment_method,
        )
        .all()
    )

    payment_total = 0
    refund_total = 0
    payment_count = 0
    refund_count = 0
    by_method_map: dict = defaultdict(lambda: {"payment": 0, "refund": 0, "count": 0})
    for rec_type, method, cnt, amt in rows:
        amt_int = int(amt or 0)
        cnt_int = int(cnt or 0)
        method_key = method or "未指定"
        if rec_type == "payment":
            payment_total += amt_int
            payment_count += cnt_int
            by_method_map[method_key]["payment"] += amt_int
        else:
            refund_total += amt_int
            refund_count += cnt_int
            by_method_map[method_key]["refund"] += amt_int
        by_method_map[method_key]["count"] += cnt_int

    by_method_list = [
        {
            "method": method_key,
            "payment": data["payment"],
            "refund": data["refund"],
            "count": data["count"],
        }
        for method_key, data in sorted(by_method_map.items())
    ]
    by_method_net = {
        method_key: data["payment"] - data["refund"]
        for method_key, data in by_method_map.items()
    }

    return {
        "date": target_date.isoformat(),
        "payment_total": payment_total,
        "refund_total": refund_total,
        "net": payment_total - refund_total,
        "payment_count": payment_count,
        "refund_count": refund_count,
        "transaction_count": payment_count + refund_count,
        "by_method": by_method_list,
        "by_method_net": by_method_net,
    }
