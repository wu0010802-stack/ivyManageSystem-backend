"""每日用藥提醒排程。

每天台北時區 07:30（預設，可由環境變數覆寫）掃描當日 `student_medication_orders`，
計算各班導師的「今日待辦用藥」彙總，推送至 notification summary 快取，讓各教師在登入
主頁時即可看到當日需餵藥任務摘要。

- 排程：asyncio-based，沿用其他 scheduler 的寫法（如 graduation_scheduler）
- 單 worker 啟用：`MEDICATION_REMINDER_ENABLED=1`
- idempotent：以 last_run_date 追蹤，同一日不會觸發第二次；游標持久化於
  scheduler_watermarks（utils/scheduler_watermark），同日重啟不重發、
  重啟時過點且游標落後則補跑一次

注意：Notification Center 本身已有個人化快取（見 services/dashboard_query_service.py），
本排程的職責只是「強制刷新今日用藥區塊」，真正的彙總查詢由 dashboard_query_service 即時算。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists

from config import settings
from models.classroom import (
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
    Student,
)
from models.database import session_scope
from models.portfolio import StudentMedicationOrder
from models.student_leave import StudentLeaveRequest
from utils.scheduler_observability import record_rows, scheduler_iteration
from utils.scheduler_watermark import get_watermark, set_watermark

logger = logging.getLogger(__name__)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# 持久化時間游標 name（scheduler_watermarks 表；比照 announcement_publish）
_WATERMARK_NAME = "medication_reminder"

# 觸發時刻（可由環境變數覆寫）
REMINDER_HOUR = settings.scheduler.medication_reminder_hour
REMINDER_MINUTE = settings.scheduler.medication_reminder_minute

# 檢查週期：每 5 分鐘巡檢一次（寬容度夠又不會太頻繁）
CHECK_INTERVAL_SECONDS = settings.scheduler.medication_reminder_check_interval

# heartbeat expected interval：本 job 每日只跑一次，傳「一天」而非巡檢週期，否則
# /health/schedulers 在跑完當日那次後即誤判 lagging → 永久 503（比照 data_quality）。
_DAILY_INTERVAL_SEC = 24 * 60 * 60


def _now_taipei() -> datetime:
    return datetime.now(TAIPEI_TZ)


def _today_target_dt(today: date) -> datetime:
    """今日觸發時刻（台北時區）。"""
    return datetime.combine(
        today,
        time(hour=REMINDER_HOUR, minute=REMINDER_MINUTE),
        tzinfo=TAIPEI_TZ,
    )


def _watermark_dt(today: date) -> datetime:
    """游標持久化值：以當日觸發時刻（naive Taipei）記錄，date 部分 = 成功跑的日期。"""
    return datetime.combine(today, time(hour=REMINDER_HOUR, minute=REMINDER_MINUTE))


def _load_last_run_date(session) -> Optional[date]:
    """讀持久化游標的 date 部分；未設定回 None（首次啟動 → 過點保險跑一次）。"""
    ts = get_watermark(session, _WATERMARK_NAME)
    return ts.date() if ts else None


def _active_orders_query(session, today: date):
    """今日須提醒的 medication orders（排除已核准請假學生 + 終態學生）。

    請假學生今日不在校，提醒會誤導家長並可能在接 LINE 推播後形成誤推。
    終態學生（已畢業/退學/轉出）已永久離校，同理不該再觸發用藥提醒——PII GC
    前殘留的當日 order 否則會持續誤推給班導。對稱於上方 leave 排除。
    """
    leave_subq = exists().where(
        and_(
            StudentLeaveRequest.student_id == StudentMedicationOrder.student_id,
            StudentLeaveRequest.status == "approved",
            StudentLeaveRequest.start_date <= today,
            StudentLeaveRequest.end_date >= today,
        )
    )
    terminal_subq = exists().where(
        and_(
            Student.id == StudentMedicationOrder.student_id,
            Student.lifecycle_status.in_(
                [LIFECYCLE_GRADUATED, LIFECYCLE_TRANSFERRED, LIFECYCLE_WITHDRAWN]
            ),
        )
    )
    return (
        session.query(StudentMedicationOrder)
        .filter(StudentMedicationOrder.order_date == today)
        .filter(~leave_subq)
        .filter(~terminal_subq)
    )


def count_today_medication_orders(today: Optional[date] = None) -> int:
    """查詢今日有用藥需求的學生數量，回傳 order count。

    由 run_medication_reminder() 呼叫；也可供單元測試直接驗證查詢邏輯。
    """
    today = today or _now_taipei().date()
    with session_scope() as session:
        return _active_orders_query(session, today).count()


def run_medication_reminder(effective_date: Optional[date] = None) -> dict:
    """執行一次「今日用藥提醒」。

    目前行為：
    - 多 worker 互斥：advisory lock 保證同日只觸發一次（取不到鎖直接略過）
    - 掃描今日 orders，log 總數
    - invalidate dashboard_query_service 的 notification 快取（若有現成 API）
    - 之後如要接 LINE 推播 / webhook，這裡加

    回傳：{"date": ..., "order_count": ...}（取不到鎖時 skipped=True）
    """
    from utils.advisory_lock import try_scheduler_lock

    today = effective_date or _now_taipei().date()
    with session_scope() as session:
        with try_scheduler_lock(
            session,
            scheduler_name="medication_reminder",
            run_key=today.isoformat(),
        ) as acquired:
            if not acquired:
                logger.info(
                    "用藥提醒：已有其他 worker 在執行 date=%s，本次略過",
                    today.isoformat(),
                )
                return {
                    "date": today.isoformat(),
                    "order_count": 0,
                    "skipped": True,
                }
            try:
                order_count = _active_orders_query(session, today).count()
            except Exception:
                # Downgraded：scheduler 端 wrapper 會做 throttled Sentry 上報
                logger.warning("用藥提醒查詢失敗", exc_info=True)
                raise
            # 游標與本次執行在同一事務原子落地（session_scope 退出時 commit）：
            # 同日重啟讀到游標 = 今日 → 不重發；失敗 rollback → 下輪巡檢重跑。
            set_watermark(session, _WATERMARK_NAME, _watermark_dt(today))

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

    以 last_run_date 避免同日重複觸發；游標持久化於 scheduler_watermarks
    （比照 announcement_publish_scheduler）：
    - 同日重啟：seed 讀回今日游標 → 不重發
    - 重啟時目標時刻已過且游標落後（昨日或更早）：補跑一次
    - 游標讀取失敗：fallback None（與舊行為同 → 過點保險再跑一次，
      advisory lock + 多 worker 下另一 worker 的游標仍會擋重複）
    """
    last_run_date: Optional[date] = None
    try:
        with session_scope() as session:
            last_run_date = _load_last_run_date(session)
    except Exception:  # noqa: BLE001 — seed 失敗不可擋 loop 啟動
        logger.warning("用藥提醒 watermark 讀取失敗，視為今日未跑", exc_info=True)
    if last_run_date is not None:
        logger.info("用藥提醒 watermark seed：上次成功日 %s", last_run_date.isoformat())
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
                with scheduler_iteration(
                    "medication_reminder",
                    expected_interval_seconds=_DAILY_INTERVAL_SEC,
                ):
                    result = await asyncio.to_thread(
                        run_medication_reminder, effective_date=today
                    )
                    record_rows(
                        "medication_reminder",
                        int(result.get("order_count", 0) or 0),
                    )
                    last_run_date = today
        except Exception:
            logger.exception("用藥提醒巡檢失敗（忽略本次）")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
