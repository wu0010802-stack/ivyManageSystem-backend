"""員工請假 → 考勤同步單一進入點。

對齊學生端 services/student_leave_service 的設計理念,但因員工請假支援半天/小時,
寫入策略採「並存模式」:全天 upsert status=LEAVE;半天/小時保留打卡並標記 leave_record_id。
"""

from datetime import date, time, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from models.approval import ApprovalStatus
from models.attendance import Attendance, AttendanceStatus
from models.leave import LeaveRecord
from utils.attendance_calc import (
    compute_late_minutes_with_leave,
    compute_early_leave_minutes_with_leave,
    sync_attendance_flags,
)

# 員工無自訂排班時的 fallback（對齊 Employee.work_start_time/work_end_time 欄位預設）
DEFAULT_SCHEDULED_START = time(8, 0)
DEFAULT_SCHEDULED_END = time(17, 0)

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


def _span_days(leave: LeaveRecord) -> int:
    """請假涵蓋天數（含頭尾）。供多日部分假 per-day 攤分用，最小 1。"""
    return max(1, (leave.end_date - leave.start_date).days + 1)


def _per_day_partial_hours(leave: LeaveRecord) -> Decimal:
    """多日部分假 per-day 攤分時數 = leave_hours / span_days（單日 span=1 不變）。

    F-B：原本每天各寫整筆 leave_hours，薪資逐列加總 → N 天 × leave_hours
    （扣薪乘以天數），與曠職側 engine._compute_absence 的 per_day=lv_hours/span_days
    口徑相反。改為攤分，總和回到單筆 leave_hours。
    """
    return Decimal(str(leave.leave_hours)) / Decimal(_span_days(leave))


def _parse_hhmm(s: str | None) -> time | None:
    """解析 "HH:MM" 字串成 time；None 或格式不合一律回 None。

    防護：work_start_time/work_end_time 為 String(5) 無 DB CHECK，畸形舊資料
    （"0800"/"8"/"08:30:00"/"25:00" 等）原本會在 split(":") 解包或 int()/time()
    raise ValueError → 上游請假寫考勤路徑 500。解析失敗回 None 與「None 字串回 None」
    語義一致，呼叫端（`or DEFAULT_*` / compute_*_with_leave）對 None 已有 fallback。
    """
    if s is None:
        return None
    try:
        hh, mm = s.split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError):
        return None


def _get_employee_schedule(session: Session, employee_id: int) -> tuple[time, time]:
    """取員工實際排班 work_start_time/work_end_time（HH:MM 字串），缺值 fallback。

    P1-3：原本一律回硬編 09:00/18:00，無視員工 work_end_time（預設 08:00-17:00），
    導致部分請假日 17:00 正常下班被誤判 60 分早退 → 早退扣款灌水。改讀員工實際排班，
    與 utils/attendance_leave_merge._get_employee_schedule 對齊。
    """
    from models.employee import Employee

    emp = session.query(Employee).filter_by(id=employee_id).first()
    start = (
        _parse_hhmm(getattr(emp, "work_start_time", None)) or DEFAULT_SCHEDULED_START
    )
    end = _parse_hhmm(getattr(emp, "work_end_time", None)) or DEFAULT_SCHEDULED_END
    return start, end


# ── 公開 API ──────────────────────────────────────────────────────


def apply(session: Session, leave_id: int) -> list[date]:
    """把 approved leave 寫入 Attendance。Idempotent。

    Pre-condition: leave 必須是 status='approved'；否則 raise LeaveNotApproved。
    回傳實際寫入的日期列表。
    """
    leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
    if leave is None:
        raise LeaveNotApproved(f"leave_id={leave_id} 不存在")
    if leave.status != ApprovalStatus.APPROVED.value:
        raise LeaveNotApproved(f"leave_id={leave_id} 不是已核可(status={leave.status})")

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
    """全天請假寫考勤。

    F-A（業主裁定語意）：全天假當天若有真實打卡 → **視同銷假/正常上班**
    （員工實際有來上班）：保留打卡、partial_leave_hours=0、status 依打卡重算、
    leave_record_id 仍連結（供追溯但 0 扣款，薪資 _hours 對 status≠LEAVE 且
    partial=0 回 0）。**不可清 punch**（清打卡會永久銷毀真實出勤資料）。
    只有「全天假且當天完全無真實打卡」才 status=LEAVE、扣 8h。

    與 utils/attendance_leave_merge case-2/3 對齊，消除「誰最後寫考勤」決定
    扣 8h 或 0h 的分叉（先核假後匯打卡會靜默漏扣整日假薪）。
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

    # 衝突 guard:row 已被別筆 leave 佔據（先於冪等判斷，避免覆寫別筆假）
    if row.leave_record_id is not None and row.leave_record_id != leave.id:
        raise LeaveAttendanceConflict(
            f"{d} employee_id={leave.employee_id} 已有 leave_record_id="
            f"{row.leave_record_id},無法覆蓋為 leave_id={leave.id}"
        )

    has_real_punch = row.punch_in_time is not None or row.punch_out_time is not None

    if has_real_punch:
        # 視同銷假：保留打卡、partial=0、status 依打卡重算、leave_record_id 連結。
        # 冪等：第二次跑時 partial 已是 0、status 已依打卡算好 → 重算結果相同。
        row.leave_record_id = leave.id
        row.partial_leave_hours = Decimal("0")

        sched_start, sched_end = _get_employee_schedule(session, leave.employee_id)

        if row.punch_in_time is not None:
            punch_in_time_only = row.punch_in_time.time()
            row.late_minutes = compute_late_minutes_with_leave(
                punch_in=punch_in_time_only,
                scheduled_start=sched_start,
                leave_start=None,
                leave_end=None,
            )
        else:
            row.late_minutes = 0

        if row.punch_out_time is not None:
            # 跨夜班：下班落在隔日，不可用 .time() 截斷後當早退（P1-4）
            if row.punch_out_time.date() > d:
                row.early_leave_minutes = 0
            else:
                punch_out_time_only = row.punch_out_time.time()
                row.early_leave_minutes = compute_early_leave_minutes_with_leave(
                    punch_out=punch_out_time_only,
                    scheduled_end=sched_end,
                    leave_start=None,
                    leave_end=None,
                )
        else:
            row.early_leave_minutes = 0

        late_min = row.late_minutes or 0
        early_min = row.early_leave_minutes or 0
        if late_min > 0:
            row.status = AttendanceStatus.LATE.value
        elif early_min > 0:
            row.status = AttendanceStatus.EARLY_LEAVE.value
        else:
            row.status = AttendanceStatus.NORMAL.value

        sync_attendance_flags(row)
        return

    # 無真實打卡 → 原語意：status=LEAVE、扣 8h
    # Idempotent guard:已是本筆 leave 寫的 → no-op
    if row.leave_record_id == leave.id and row.status == AttendanceStatus.LEAVE.value:
        return

    row.status = AttendanceStatus.LEAVE.value
    row.punch_in_time = None
    row.punch_out_time = None
    row.late_minutes = 0
    row.early_leave_minutes = 0
    row.leave_record_id = leave.id
    row.partial_leave_hours = None
    # P0-3：全天假覆蓋後清掉殘留的遲到/早退/缺卡旗標，否則薪資仍算成缺卡
    sync_attendance_flags(row)


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
    # F-B：多日部分假 per-day 攤分，避免每天各寫整筆 leave_hours 導致扣薪乘以天數
    row.partial_leave_hours = _per_day_partial_hours(leave)

    # 解析 leave start/end time（String "HH:MM" → time）
    lv_start = _parse_hhmm(leave.start_time)
    lv_end = _parse_hhmm(leave.end_time)

    # 無打卡 → status=ABSENT
    if row.punch_in_time is None and row.punch_out_time is None:
        row.status = AttendanceStatus.ABSENT.value
        row.late_minutes = 0
        row.early_leave_minutes = 0
        sync_attendance_flags(row)
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
        # 跨夜班：下班落在隔日，不可用 .time() 截斷後當早退（會誤算 ~960 分，P1-4）
        if row.punch_out_time.date() > d:
            row.early_leave_minutes = 0
        else:
            punch_out_time_only = row.punch_out_time.time()
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

    # P0-3：重算後同步布林旗標
    sync_attendance_flags(row)


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
                # 跨夜班：下班落在隔日，不可用 .time() 截斷後當早退（P1-4）
                if row.punch_out_time.date() > d:
                    early_min = 0
                    row.early_leave_minutes = 0
                else:
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

            # P0-3：清 leave 還原後同步布林旗標
            sync_attendance_flags(row)

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
    if leave is None or leave.status != ApprovalStatus.APPROVED.value:
        return reverted, []

    applied = apply(session, leave_id)
    return reverted, applied
