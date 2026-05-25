"""員工請假 → 考勤同步單一進入點。

對齊學生端 services/student_leave_service 的設計理念,但因員工請假支援半天/小時,
寫入策略採「並存模式」:全天 upsert status=LEAVE;半天/小時保留打卡並標記 leave_record_id。
"""

from datetime import date, time, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from models.attendance import Attendance, AttendanceStatus
from models.leave import LeaveRecord
from utils.attendance_calc import (
    compute_late_minutes_with_leave,
    compute_early_leave_minutes_with_leave,
)

# 預設排班（若員工無自訂排班則 fallback）
DEFAULT_SCHEDULED_START = time(9, 0)
DEFAULT_SCHEDULED_END = time(18, 0)

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


def _parse_hhmm(s: str | None) -> time | None:
    """解析 "HH:MM" 字串成 time，None 回傳 None。"""
    if s is None:
        return None
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def _get_employee_schedule(session: Session, employee_id: int) -> tuple[time, time]:
    """取員工排班上下班時間。若員工 model 無欄位則 fallback 預設。

    plan 階段 simplest：先 fallback default。若日後員工 model 加排班欄，改這裡。
    """
    return DEFAULT_SCHEDULED_START, DEFAULT_SCHEDULED_END


# ── 公開 API ──────────────────────────────────────────────────────


def apply(session: Session, leave_id: int) -> list[date]:
    """把 approved leave 寫入 Attendance。Idempotent。

    Pre-condition: leave 必須是 is_approved=True；否則 raise LeaveNotApproved。
    回傳實際寫入的日期列表。
    """
    leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
    if leave is None:
        raise LeaveNotApproved(f"leave_id={leave_id} 不存在")
    if leave.is_approved is not True:
        raise LeaveNotApproved(
            f"leave_id={leave_id} 不是已核可(is_approved={leave.is_approved})"
        )

    _assert_leave_time_consistent(leave)

    written: list[date] = []
    for d in _iter_dates(leave):
        if _is_full_day(leave):
            _apply_full_day(session, leave, d)
        else:
            _apply_partial(session, leave, d)  # Task 7 補完
        written.append(d)
    return written


def _apply_full_day(session: Session, leave: LeaveRecord, d: date) -> None:
    """全天:upsert status=LEAVE,清打卡,leave_record_id 寫入。"""
    row = (
        session.query(Attendance)
        .filter_by(
            employee_id=leave.employee_id,
            attendance_date=d,
        )
        .first()
    )

    if row is None:
        row = Attendance(
            employee_id=leave.employee_id,
            attendance_date=d,
        )
        session.add(row)

    # Idempotent guard:已是本筆 leave 寫的 → no-op
    if row.leave_record_id == leave.id and row.status == AttendanceStatus.LEAVE.value:
        return

    # 衝突 guard:row 已被別筆 leave 佔據
    if row.leave_record_id is not None and row.leave_record_id != leave.id:
        raise LeaveAttendanceConflict(
            f"{d} employee_id={leave.employee_id} 已有 leave_record_id="
            f"{row.leave_record_id},無法覆蓋為 leave_id={leave.id}"
        )

    row.status = AttendanceStatus.LEAVE.value
    row.punch_in_time = None
    row.punch_out_time = None
    row.late_minutes = 0
    row.early_leave_minutes = 0
    row.leave_record_id = leave.id
    row.partial_leave_hours = None


def _apply_partial(session: Session, leave: LeaveRecord, d: date) -> None:
    """半天/小時：UPSERT 不覆蓋 punch_in/punch_out；
    leave_record_id + partial_leave_hours 寫入；
    late_minutes/early_leave_minutes 用 leave-aware 重算。
    """
    row = (
        session.query(Attendance)
        .filter_by(
            employee_id=leave.employee_id,
            attendance_date=d,
        )
        .first()
    )

    if row is None:
        row = Attendance(
            employee_id=leave.employee_id,
            attendance_date=d,
        )
        session.add(row)

    # Idempotent guard：已是本筆 leave 寫的且 partial_leave_hours 已填 → no-op
    if row.leave_record_id == leave.id and row.partial_leave_hours is not None:
        return

    # 衝突 guard：row 已被別筆 leave 佔據
    if row.leave_record_id is not None and row.leave_record_id != leave.id:
        raise LeaveAttendanceConflict(
            f"{d} employee_id={leave.employee_id} 已有 leave_record_id="
            f"{row.leave_record_id}，無法新寫入 leave_id={leave.id}"
        )

    row.leave_record_id = leave.id
    row.partial_leave_hours = Decimal(str(leave.leave_hours))

    # 解析 leave start/end time（String "HH:MM" → time）
    lv_start = _parse_hhmm(leave.start_time)
    lv_end = _parse_hhmm(leave.end_time)

    # 無打卡 → status=ABSENT
    if row.punch_in_time is None and row.punch_out_time is None:
        row.status = AttendanceStatus.ABSENT.value
        row.late_minutes = 0
        row.early_leave_minutes = 0
        return

    # 有打卡 → 用 leave-aware 重算 late/early_leave
    sched_start, sched_end = _get_employee_schedule(session, leave.employee_id)

    if row.punch_in_time is not None:
        # punch_in_time 是 DateTime，需先轉成 time
        punch_in_time_only = row.punch_in_time.time() if row.punch_in_time else None
        row.late_minutes = compute_late_minutes_with_leave(
            punch_in=punch_in_time_only,
            scheduled_start=sched_start,
            leave_start=lv_start,
            leave_end=lv_end,
        )

    if row.punch_out_time is not None:
        # punch_out_time 是 DateTime，需先轉成 time
        punch_out_time_only = row.punch_out_time.time() if row.punch_out_time else None
        row.early_leave_minutes = compute_early_leave_minutes_with_leave(
            punch_out=punch_out_time_only,
            scheduled_end=sched_end,
            leave_start=lv_start,
            leave_end=lv_end,
        )

    # 若 late 與 early_leave 都歸零，且原 status 為 LATE/EARLY_LEAVE → 退回 NORMAL
    late_min = row.late_minutes or 0
    early_min = row.early_leave_minutes or 0
    if late_min == 0 and early_min == 0:
        if row.status in (
            AttendanceStatus.LATE.value,
            AttendanceStatus.EARLY_LEAVE.value,
        ):
            row.status = AttendanceStatus.NORMAL.value


def revert(session: Session, leave_id: int) -> list[date]:
    """把 leave 對 Attendance 的影響還原。Idempotent。

    - 全天且無 punch → 刪除 row
    - 全天有 punch（髒資料）→ 清 leave_*，status 退回 NORMAL，重算 late/early
    - 部分假 → 清 leave_record_id / partial_leave_hours，status 退回 NORMAL，
               重算 late/early（無 leave 加成）
    回傳實際處理的日期列表。若無任何 row（已是 no-op 狀態）回傳 []。
    """
    rows = (
        session.query(Attendance).filter(Attendance.leave_record_id == leave_id).all()
    )

    reverted: list[date] = []
    for row in rows:
        d = row.attendance_date

        has_punch = row.punch_in_time is not None or row.punch_out_time is not None

        if not has_punch:
            # 無打卡 → 直接刪除 row
            session.delete(row)
        else:
            # 有打卡 → 保留 punch，清 leave_* 欄位，重算 late/early
            row.leave_record_id = None
            row.partial_leave_hours = None

            sched_start, sched_end = _get_employee_schedule(session, row.employee_id)

            late_min = 0
            if row.punch_in_time is not None:
                punch_in_time_only = row.punch_in_time.time()
                late_min = compute_late_minutes_with_leave(
                    punch_in=punch_in_time_only,
                    scheduled_start=sched_start,
                    leave_start=None,
                    leave_end=None,
                )
                row.late_minutes = late_min

            early_min = 0
            if row.punch_out_time is not None:
                punch_out_time_only = row.punch_out_time.time()
                early_min = compute_early_leave_minutes_with_leave(
                    punch_out=punch_out_time_only,
                    scheduled_end=sched_end,
                    leave_start=None,
                    leave_end=None,
                )
                row.early_leave_minutes = early_min

            # 根據重算結果決定 status
            if late_min > 0:
                row.status = AttendanceStatus.LATE.value
            elif early_min > 0:
                row.status = AttendanceStatus.EARLY_LEAVE.value
            else:
                row.status = AttendanceStatus.NORMAL.value

        reverted.append(d)

    return reverted


def reapply(
    session: Session,
    leave_id: int,
    old_snapshot: dict | None = None,
) -> tuple[list[date], list[date]]:
    """update_leave 改了關鍵欄（日期/時段/leave_type/hours）時呼叫。

    內部組合：revert（舊範圍）→ apply（新範圍）。
    old_snapshot 必須由 caller 在 model 寫回前抓：
      {start_date, end_date, start_time, end_time, leave_type, leave_hours}
    revert 純靠 row.leave_record_id 找，不需要 old_snapshot 也能清舊 row；
    但 API 仍保留 old_snapshot 參數供 hook（plan Task 13）在 model 寫回前 snapshot。
    """
    reverted = revert(session, leave_id)

    leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
    if leave is None or leave.is_approved is not True:
        return reverted, []

    applied = apply(session, leave_id)
    return reverted, applied
