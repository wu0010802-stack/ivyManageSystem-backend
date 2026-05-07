"""
services/finance_reconciliation_scheduler.py — 才藝 POS paid_amount 每日對帳 cron。

業務語意（spec H4）：每天 02:00 Asia/Taipei 掃描所有 active registrations，
比對 paid_amount vs payment_records 淨額；若發現不一致 → 推 LINE 警示給
老闆。配合 services/finance_reconciliation_service.py 的純函式偵測 helper。

排程模式沿用其他 scheduler（graduation_scheduler / salary_snapshot_scheduler）：
- asyncio loop + stop_event
- env var FINANCE_RECONCILIATION_ENABLED 控制 opt-in（預設關閉）
- advisory lock 保證多 worker 部署時同日只執行一次

Why opt-in：本模組會每日推 LINE 訊息；無 LINE 設定的環境（dev / 早期生產）
不該被打擾，故預設關閉。生產確認 LINE bot 設定 OK 後再啟用。
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from models.database import get_session

logger = logging.getLogger(__name__)
TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# 每日觸發時間（Asia/Taipei）
TARGET_HOUR = 2
TARGET_MINUTE = 0

# 巡檢週期：每分鐘檢查一次是否到目標時間（精度 1 分鐘對日報來說綽綽有餘）
CHECK_INTERVAL_SECONDS = 60


def scheduler_enabled() -> bool:
    return os.getenv("FINANCE_RECONCILIATION_ENABLED", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _default_line_push(text: str) -> bool:
    """Default LINE push 來源：main.py 創建的 line_service 單例。

    Lazy import 以避免測試時要載 main.py（含 router 註冊等）。
    """
    try:
        from main import line_service
    except Exception:
        return False
    try:
        return bool(line_service._push(text))
    except Exception:
        logger.exception("LINE 推送失敗")
        return False


def run_finance_reconciliation(target_date=None, line_push=None) -> dict:
    """執行對帳；回傳統計摘要。可手動觸發（測試 / CLI）。

    Args:
        target_date: 紀錄日期（預設今日 Asia/Taipei）；scheduler 不需要傳，CLI/測試可指定
        line_push: 推 LINE 函式，簽章 (text: str) -> bool；測試時可注入 mock

    多 worker 啟用時用 advisory lock 保證同一日期只有一個 worker 真正執行；
    其他 worker 取不到鎖即略過（回傳 skipped 標記，不算失敗）。
    """
    from services.finance_reconciliation_service import (
        detect_paid_amount_mismatches,
        format_mismatches_for_line,
    )
    from utils.advisory_lock import try_scheduler_lock

    if line_push is None:
        line_push = _default_line_push

    today = target_date or datetime.now(TAIPEI_TZ).date()
    session = get_session()
    try:
        with try_scheduler_lock(
            session,
            scheduler_name="finance_reconciliation",
            run_key=today.isoformat(),
        ) as acquired:
            if not acquired:
                logger.info(
                    "對帳排程：已有其他 worker 在執行 date=%s，本次略過",
                    today.isoformat(),
                )
                return {"date": today.isoformat(), "skipped": True}

            mismatches = detect_paid_amount_mismatches(session)
            count = len(mismatches)
            total_drift = sum(m.drift for m in mismatches)
            logger.warning(
                "對帳完成 date=%s mismatches=%d total_drift=%d",
                today.isoformat(),
                count,
                total_drift,
            )

            notification_pushed = False
            if mismatches:
                # 帳不一致才推訊息（避免每天都送「一切正常」雜訊）
                msg = format_mismatches_for_line(mismatches, today.isoformat())
                try:
                    notification_pushed = bool(line_push(msg))
                except Exception:
                    logger.exception("LINE 對帳通知推送失敗")
                    notification_pushed = False

            return {
                "date": today.isoformat(),
                "mismatch_count": count,
                "total_drift": total_drift,
                "notification_pushed": notification_pushed,
            }
    finally:
        session.close()


async def run_finance_reconciliation_scheduler(stop_event: asyncio.Event) -> None:
    """每日 02:00 Asia/Taipei 觸發對帳。idempotent：每日只跑一次。"""
    logger.info(
        "對帳排程啟動（每日 %02d:%02d Asia/Taipei，巡檢週期 %ss）",
        TARGET_HOUR,
        TARGET_MINUTE,
        CHECK_INTERVAL_SECONDS,
    )
    last_run_date: Optional["__import__('datetime').date"] = None
    while not stop_event.is_set():
        try:
            now = datetime.now(TAIPEI_TZ)
            if (
                now.hour == TARGET_HOUR
                and now.minute == TARGET_MINUTE
                and last_run_date != now.date()
            ):
                logger.warning("觸發對帳排程 date=%s", now.date().isoformat())
                try:
                    run_finance_reconciliation()
                    last_run_date = now.date()
                except Exception:
                    logger.exception("對帳本次失敗，將於下次巡檢重試")
        except Exception:
            logger.exception("對帳巡檢失敗（忽略本次）")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
