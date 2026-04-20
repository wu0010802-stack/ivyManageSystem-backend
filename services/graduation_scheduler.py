"""學年末自動畢業排程。

每年下學期結束日（預設 7/31，台北時區）凌晨自動將畢業班（`class_grades.is_graduation_grade=True`）
的在讀學生 (lifecycle_status=active) 轉為 graduated。

- 透過 `StudentLifecycleService.transition()` 統一流程：寫 change_log、
  取消接送通知、軟刪才藝報名、同步 is_active/status/graduation_date。
- 單 worker 啟用 (`AUTO_GRADUATION_ENABLED=1`)；避免多 worker 重複畢業。
- idempotent：當日已跑過就略過；transition() 本身也會拒絕終態→終態。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from models.classroom import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_GRADUATED,
    ClassGrade,
    Classroom,
    Student,
)
from models.database import get_session
from services.student_lifecycle import (
    LifecycleTransitionError,
    transition as lifecycle_transition,
)

logger = logging.getLogger(__name__)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# 學年結束日（下學期最後一天）— 目前為幼兒園常態 7/31
GRADUATION_MONTH = int(os.getenv("AUTO_GRADUATION_MONTH", "7"))
GRADUATION_DAY = int(os.getenv("AUTO_GRADUATION_DAY", "31"))

# 預告期間：畢業日前 N 天顯示「即將畢業」提示（給 notification 聚合用）
PREVIEW_WINDOW_DAYS = int(os.getenv("AUTO_GRADUATION_PREVIEW_DAYS", "7"))

# 檢查週期：每天檢查一次即可；此處容錯用 1 小時巡檢以降低 miss 機率
CHECK_INTERVAL_SECONDS = int(os.getenv("AUTO_GRADUATION_CHECK_INTERVAL", "3600"))


def _today_taipei() -> date:
    return datetime.now(TAIPEI_TZ).date()


def graduation_date_for_year(year: int) -> date:
    return date(year, GRADUATION_MONTH, GRADUATION_DAY)


def upcoming_graduation_date(today: Optional[date] = None) -> date:
    """今年或明年的畢業日（未過則是今年，已過則是明年）。"""
    today = today or _today_taipei()
    this_year = graduation_date_for_year(today.year)
    return this_year if today <= this_year else graduation_date_for_year(today.year + 1)


def is_within_preview_window(today: Optional[date] = None) -> bool:
    """今天是否落在畢業日前 PREVIEW_WINDOW_DAYS 天內（含畢業日當天）。"""
    today = today or _today_taipei()
    target = graduation_date_for_year(today.year)
    return target - timedelta(days=PREVIEW_WINDOW_DAYS) <= today <= target


def list_upcoming_graduates(session) -> list[Student]:
    """列出目前在讀且班級屬於畢業班年級的學生。"""
    return (
        session.query(Student)
        .join(Classroom, Classroom.id == Student.classroom_id)
        .join(ClassGrade, ClassGrade.id == Classroom.grade_id)
        .filter(
            ClassGrade.is_graduation_grade.is_(True),
            Student.lifecycle_status == LIFECYCLE_ACTIVE,
        )
        .all()
    )


def run_auto_graduation(effective_date: Optional[date] = None) -> dict:
    """執行自動畢業；回傳統計摘要。可手動觸發（供測試 / CLI）。"""
    effective_date = effective_date or graduation_date_for_year(_today_taipei().year)
    session = get_session()
    succeeded = 0
    failed: list[dict] = []
    try:
        candidates = list_upcoming_graduates(session)
        logger.info("自動畢業：找到畢業候選 %s 位", len(candidates))
        for student in candidates:
            try:
                lifecycle_transition(
                    session,
                    student,
                    to_status=LIFECYCLE_GRADUATED,
                    effective_date=effective_date,
                    reason="正常畢業",
                    notes=f"系統自動畢業（{effective_date.isoformat()}）",
                    recorded_by=None,
                )
                # 同步後端副作用（才藝報名軟刪、接送通知取消）
                try:
                    from api.activity._shared import (
                        sync_registrations_on_student_deactivate,
                    )

                    sync_registrations_on_student_deactivate(session, student.id)
                except Exception:
                    logger.exception(
                        "自動畢業同步才藝報名失敗 student_id=%s", student.id
                    )
                succeeded += 1
            except LifecycleTransitionError as exc:
                failed.append({"student_id": student.id, "reason": str(exc)})
                logger.warning("自動畢業略過 student_id=%s：%s", student.id, exc)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("自動畢業執行失敗")
        raise
    finally:
        session.close()

    result = {
        "effective_date": effective_date.isoformat(),
        "succeeded": succeeded,
        "failed": failed,
        "total_candidates": succeeded + len(failed),
    }
    logger.warning("自動畢業完成：%s", result)
    return result


def scheduler_enabled() -> bool:
    return os.getenv("AUTO_GRADUATION_ENABLED", "").lower() in ("1", "true", "yes")


async def run_auto_graduation_scheduler(stop_event: asyncio.Event) -> None:
    """每日檢查；符合條件即執行。idempotent：每個學年只跑一次。"""
    logger.info(
        "自動畢業排程啟動（畢業日 %s/%s，台北時區，巡檢週期 %ss）",
        GRADUATION_MONTH,
        GRADUATION_DAY,
        CHECK_INTERVAL_SECONDS,
    )
    last_run_year: Optional[int] = None
    while not stop_event.is_set():
        try:
            today = _today_taipei()
            target = graduation_date_for_year(today.year)
            if today == target and last_run_year != today.year:
                logger.warning("觸發自動畢業（date=%s）", today.isoformat())
                try:
                    run_auto_graduation(effective_date=today)
                    last_run_year = today.year
                except Exception:
                    logger.exception("自動畢業本次失敗，將於下次巡檢重試")
        except Exception:
            logger.exception("自動畢業巡檢失敗（忽略本次）")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
