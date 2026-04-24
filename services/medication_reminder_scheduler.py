"""每日用藥提醒排程。

每天台北時區 07:30（預設，可由環境變數覆寫）掃描當日 `student_medication_orders`，
計算各班導師的「今日待辦用藥」彙總，推送至 notification summary 快取，讓各教師在登入
主頁時即可看到當日需餵藥任務摘要。

- 排程：asyncio-based，沿用其他 scheduler 的寫法（如 graduation_scheduler）
- 單 worker 啟用：`MEDICATION_REMINDER_ENABLED=1`
- idempotent：以 last_run_date 追蹤，同一日不會觸發第二次

注意：Notification Center 本身已有個人化快取（見 services/dashboard_query_service.py），
本排程的職責只是「強制刷新今日用藥區塊」，真正的彙總查詢由 dashboard_query_service 即時算。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from models.database import session_scope
from models.portfolio import StudentMedicationOrder

logger = logging.getLogger(__name__)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# 觸發時刻（可由環境變數覆寫）
REMINDER_HOUR = int(os.getenv("MEDICATION_REMINDER_HOUR", "7"))
REMINDER_MINUTE = int(os.getenv("MEDICATION_REMINDER_MINUTE", "30"))

# 檢查週期：每 5 分鐘巡檢一次（寬容度夠又不會太頻繁）
CHECK_INTERVAL_SECONDS = int(os.getenv("MEDICATION_REMINDER_CHECK_INTERVAL", "300"))


def _now_taipei() -> datetime:
    return datetime.now(TAIPEI_TZ)


def _today_target_dt(today: date) -> datetime:
    """今日觸發時刻（台北時區）。"""
    return datetime.combine(
        today,
        time(hour=REMINDER_HOUR, minute=REMINDER_MINUTE),
        tzinfo=TAIPEI_TZ,
    )


def count_today_medication_orders(today: Optional[date] = None) -> int:
    """查詢今日有用藥需求的學生數量，回傳 order count。

    由 run_medication_reminder() 呼叫；也可供單元測試直接驗證查詢邏輯。
    """
    today = today or _now_taipei().date()
    with session_scope() as session:
        return (
            session.query(StudentMedicationOrder)
            .filter(StudentMedicationOrder.order_date == today)
            .count()
        )


def run_medication_reminder(effective_date: Optional[date] = None) -> dict:
    """執行一次「今日用藥提醒」。

    目前行為：
    - 掃描今日 orders，log 總數
    - invalidate dashboard_query_service 的 notification 快取（若有現成 API）
    - 之後如要接 LINE 推播 / webhook，這裡加

    回傳：{"date": ..., "order_count": ...}
    """
    today = effective_date or _now_taipei().date()
    try:
        order_count = count_today_medication_orders(today)
    except Exception:
        logger.exception("用藥提醒查詢失敗")
        return {"date": today.isoformat(), "order_count": -1, "error": True}

    # 嘗試 invalidate notification cache（若服務尚未初始化則略過）
    try:
        from services.dashboard_query_service import DashboardQueryService

        # 單例通常在 main.py 注入；這裡保守地不直接引用 singleton
        # 若 DashboardQueryService 後續提供 clear_notification_cache() 可在此呼叫
        clear = getattr(DashboardQueryService, "clear_notification_cache", None)
        if callable(clear):
            clear()  # type: ignore[call-arg]
    except Exception:  # pragma: no cover
        logger.debug("notification cache invalidate 略過（服務未就緒）")

    logger.warning(
        "用藥提醒觸發：date=%s order_count=%d",
        today.isoformat(),
        order_count,
    )
    return {"date": today.isoformat(), "order_count": order_count}


async def medication_reminder_loop(stop_event: asyncio.Event) -> None:
    """常駐 loop：每 CHECK_INTERVAL 秒檢查是否到觸發時刻，到了則執行一次。

    以 last_run_date 避免同日重複觸發（系統重啟時會保險再跑一次）。
    """
    last_run_date: Optional[date] = None
    logger.info(
        "用藥提醒排程啟動：每日 %02d:%02d (Asia/Taipei) 觸發",
        REMINDER_HOUR,
        REMINDER_MINUTE,
    )
    while not stop_event.is_set():
        try:
            now = _now_taipei()
            today = now.date()
            target = _today_target_dt(today)
            if now >= target and last_run_date != today:
                try:
                    run_medication_reminder(effective_date=today)
                    last_run_date = today
                except Exception:
                    logger.exception("用藥提醒本次失敗，將於下次巡檢重試")
        except Exception:
            logger.exception("用藥提醒巡檢失敗（忽略本次）")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
