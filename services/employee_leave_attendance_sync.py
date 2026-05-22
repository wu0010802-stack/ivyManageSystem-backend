"""員工請假 → 考勤同步單一進入點。

對齊學生端 services/student_leave_service 的設計理念,但因員工請假支援半天/小時,
寫入策略採「並存模式」:全天 upsert status=LEAVE;半天/小時保留打卡並標記 leave_record_id。
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from models.attendance import Attendance, AttendanceStatus
from models.leave import LeaveRecord

# ── 例外型別 ──────────────────────────────────────────────────────


class LeaveAttendanceConflict(Exception):
    """同日已有其他 leave_record_id 寫入 attendance(§1 同日多筆部分請假)。"""


class LeaveNotApproved(ValueError):
    """apply() 被呼叫時 leave 還沒 approved。"""


class LeavePartialTimeMissing(ValueError):
    """部分請假(leave_hours<8)缺 start_time/end_time,無法算 overlap。

    雙保險之一:LeaveCreate/Update validator 是第一道(Task 1),
    sync 入口再擋一次,避免 admin 直接 SQL 改 row 繞過 validator。
    """


# ── 內部 helper ───────────────────────────────────────────────────


def _is_full_day(leave: LeaveRecord) -> bool:
    """全天 = start_time/end_time 都 NULL 且 leave_hours 是 None 或 >= 8。

    舊資料可能 leave_hours=8.0 + start_time=None,也視為全天。
    """
    return (
        leave.start_time is None
        and leave.end_time is None
        and (leave.leave_hours is None or leave.leave_hours >= 8)
    )


def _assert_leave_time_consistent(leave: LeaveRecord) -> None:
    """半天/小時假必須有 start_time/end_time,否則 _apply_partial 會炸。"""
    if not _is_full_day(leave):
        if leave.start_time is None or leave.end_time is None:
            raise LeavePartialTimeMissing(
                f"leave_id={leave.id} 是部分請假(leave_hours={leave.leave_hours})"
                f"但缺 start_time/end_time"
            )


def _iter_dates(leave: LeaveRecord) -> Iterable[date]:
    d = leave.start_date
    while d <= leave.end_date:
        yield d
        d += timedelta(days=1)


# ── 公開 API(後續 Task 補完) ────────────────────────────────────


def apply(session: Session, leave_id: int) -> list[date]:
    raise NotImplementedError("Task 6/7 補完")


def revert(session: Session, leave_id: int) -> list[date]:
    raise NotImplementedError("Task 9 補完")


def reapply(
    session: Session,
    leave_id: int,
    old_snapshot: dict | None = None,
) -> tuple[list[date], list[date]]:
    raise NotImplementedError("Task 10 補完")
