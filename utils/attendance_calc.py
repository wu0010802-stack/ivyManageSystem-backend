"""考勤狀態重算 helper。

依新的 punch_in / punch_out 與員工上下班時間，重算 Attendance 的派生欄位
（is_late / is_early_leave / is_missing_punch_* / late_minutes /
early_leave_minutes / status）。

抽出原因：補打卡核准（api/punch_corrections.py:approve）原本只改 punch_in_time
/ punch_out_time 與 missing 旗標，未重算 is_late / late_minutes 等；薪資 engine
直接讀這些 boolean / int 欄位（services/salary/engine.py:2099, 2114；
services/salary_field_breakdown.py:83, 95），導致補卡通過但仍扣遲到金。

Refs: 邏輯漏洞 audit 2026-05-07 P0 (#6)。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Optional, TypedDict

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEFAULT_WORK_START = "08:00"
DEFAULT_WORK_END = "17:00"


class AttendanceStatusFields(TypedDict):
    is_late: bool
    is_early_leave: bool
    is_missing_punch_in: bool
    is_missing_punch_out: bool
    late_minutes: int
    early_leave_minutes: int
    status: str


def _parse_work_time(value: Optional[str], default: str) -> time:
    return datetime.strptime(value or default, "%H:%M").time()


def recompute_attendance_status(
    *,
    attendance_date: date,
    punch_in_time: Optional[datetime],
    punch_out_time: Optional[datetime],
    work_start_str: Optional[str],
    work_end_str: Optional[str],
) -> AttendanceStatusFields:
    """依 punch 時間與員工排班時間重算考勤派生欄位。

    跨夜班（punch_out_time < punch_in_time + days）的修正由 caller 端負責，
    helper 接收已 normalize 的 datetime。
    """
    work_start = _parse_work_time(work_start_str, DEFAULT_WORK_START)
    work_end = _parse_work_time(work_end_str, DEFAULT_WORK_END)

    is_late = False
    is_early_leave = False
    is_missing_punch_in = punch_in_time is None
    is_missing_punch_out = punch_out_time is None
    late_minutes = 0
    early_leave_minutes = 0
    status = "normal"

    if punch_in_time:
        work_start_dt = datetime.combine(attendance_date, work_start)
        if punch_in_time > work_start_dt:
            is_late = True
            late_minutes = int((punch_in_time - work_start_dt).total_seconds() / 60)
            status = "late"

    if punch_out_time:
        work_end_dt = datetime.combine(attendance_date, work_end)
        if punch_out_time < work_end_dt:
            is_early_leave = True
            early_leave_minutes = int(
                (work_end_dt - punch_out_time).total_seconds() / 60
            )
            status = "early_leave" if status == "normal" else status + "+early_leave"

    if is_missing_punch_in:
        status = "missing" if status == "normal" else status + "+missing_in"
    if is_missing_punch_out:
        status = "missing" if status == "normal" else status + "+missing_out"

    return {
        "is_late": is_late,
        "is_early_leave": is_early_leave,
        "is_missing_punch_in": is_missing_punch_in,
        "is_missing_punch_out": is_missing_punch_out,
        "late_minutes": late_minutes,
        "early_leave_minutes": early_leave_minutes,
        "status": status,
    }


def apply_attendance_status(
    attendance,
    *,
    work_start_str: Optional[str],
    work_end_str: Optional[str],
    session: Optional["Session"] = None,
) -> AttendanceStatusFields:
    """讀取 attendance.punch_in_time / punch_out_time / attendance_date 重算並寫回。

    供已有 ORM 物件的呼叫端使用（如 punch_corrections approve）。

    若傳入 session，會在重算後呼叫 merge_attendance_with_leave 讓
    leave_record_id / partial_leave_hours / late_minutes 對齊當日有效請假單。
    不傳 session 時跳過 merge（向下相容舊呼叫端）；建議新呼叫端一律傳入 session。
    """
    fields = recompute_attendance_status(
        attendance_date=attendance.attendance_date,
        punch_in_time=attendance.punch_in_time,
        punch_out_time=attendance.punch_out_time,
        work_start_str=work_start_str,
        work_end_str=work_end_str,
    )
    attendance.is_late = fields["is_late"]
    attendance.is_early_leave = fields["is_early_leave"]
    attendance.is_missing_punch_in = fields["is_missing_punch_in"]
    attendance.is_missing_punch_out = fields["is_missing_punch_out"]
    attendance.late_minutes = fields["late_minutes"]
    attendance.early_leave_minutes = fields["early_leave_minutes"]
    attendance.status = fields["status"]

    if session is not None:
        # leave-aware merge：重算後再以當日有效請假單覆寫 leave_record_id /
        # partial_leave_hours / late_minutes（請假涵蓋遲到時段時 late→0）。
        from utils.attendance_leave_merge import merge_attendance_with_leave

        merge_attendance_with_leave(attendance, session)
    else:
        logger.warning(
            "apply_attendance_status 未傳 session，跳過 leave-aware merge。"
            "建議補打卡核准等寫入路徑傳入 session 參數。"
        )

    return fields


def sync_attendance_flags(attendance) -> None:
    """依 attendance 最終 canonical 狀態重新推導四個布林旗標。

    薪資 engine（services/salary/engine.py:2313-2328）直接讀
    is_late / is_early_leave / is_missing_punch_* 做 late/early/missing count，
    因此 leave 併入（attendance_leave_merge）或 sync（employee_leave_attendance_sync）
    改動 late_minutes / early_leave_minutes / status / punch 後，必須同步這四個旗標，
    否則請假日仍被算成遲到/早退/缺卡（P0-3）。

    規則:
    - status == LEAVE（全天請假）→ 四旗標全 False（非遲到/早退/缺卡）
    - 其餘 → is_late = late_minutes>0；is_early_leave = early_leave_minutes>0；
             is_missing_punch_* = 對應 punch is None
    """
    # 延遲 import 避免 models ↔ utils 任何潛在循環
    from models.attendance import AttendanceStatus

    if attendance.status == AttendanceStatus.LEAVE.value:
        attendance.is_late = False
        attendance.is_early_leave = False
        attendance.is_missing_punch_in = False
        attendance.is_missing_punch_out = False
        return

    attendance.is_late = (attendance.late_minutes or 0) > 0
    attendance.is_early_leave = (attendance.early_leave_minutes or 0) > 0
    attendance.is_missing_punch_in = attendance.punch_in_time is None
    attendance.is_missing_punch_out = attendance.punch_out_time is None


# ── leave-aware 遲到 / 早退分鐘計算純函式 ─────────────────────────────────────


def _time_to_minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def compute_late_minutes_with_leave(
    punch_in: time,
    scheduled_start: time,
    leave_start: Optional[time],
    leave_end: Optional[time],
) -> int:
    """計算遲到分鐘,扣除請假時段涵蓋的部分。

    邏輯:
    - 無請假 → late = max(0, punch_in - scheduled_start)
    - 有請假 → 有效上班開始時間 = max(scheduled_start, leave_end if leave 涵蓋 scheduled_start else scheduled_start)
              late = max(0, punch_in - 有效上班開始時間)
    """
    sched_m = _time_to_minutes(scheduled_start)
    punch_m = _time_to_minutes(punch_in)

    if leave_start is None or leave_end is None:
        return max(0, punch_m - sched_m)

    lv_start_m = _time_to_minutes(leave_start)
    lv_end_m = _time_to_minutes(leave_end)

    # 請假涵蓋 scheduled_start → 有效上班開始 = leave_end
    if lv_start_m <= sched_m < lv_end_m:
        effective_start_m = lv_end_m
    else:
        effective_start_m = sched_m

    return max(0, punch_m - effective_start_m)


def compute_early_leave_minutes_with_leave(
    punch_out: time,
    scheduled_end: time,
    leave_start: Optional[time],
    leave_end: Optional[time],
) -> int:
    """計算早退分鐘,扣除請假時段涵蓋的部分。

    邏輯與遲到對稱:
    - 無請假 → early = max(0, scheduled_end - punch_out)
    - 請假涵蓋 scheduled_end → 有效下班結束 = leave_start
    """
    sched_m = _time_to_minutes(scheduled_end)
    punch_m = _time_to_minutes(punch_out)

    if leave_start is None or leave_end is None:
        return max(0, sched_m - punch_m)

    lv_start_m = _time_to_minutes(leave_start)
    lv_end_m = _time_to_minutes(leave_end)

    # 請假涵蓋 scheduled_end → 有效下班 = leave_start
    if lv_start_m < sched_m <= lv_end_m:
        effective_end_m = lv_start_m
    else:
        effective_end_m = sched_m

    return max(0, effective_end_m - punch_m)


def compute_shift_aware_status(
    punch_in_dt: Optional[datetime],
    punch_out_dt: Optional[datetime],
    shift_start_dt: datetime,
    shift_end_dt: datetime,
) -> tuple[bool, int, bool, int, str]:
    """以班別起迄 datetime 計算 late/early/status，與「兩筆打卡齊全」脫鉤（P1-4）。

    late 只需 punch_in、early 只需 punch_out，皆以班別時間為基準；缺的一側回 missing。
    shift_start_dt/shift_end_dt 已含跨夜處理（caller 在 shift_end<=shift_start 時 +1 日）。

    原本 Excel 匯入只在兩筆打卡都在時才套用班別時間（api/attendance/upload.py），
    晚班教師（如 13:00-22:00）漏打一筆卡 → 落回預設 08:00/17:00 算出數百分鐘假遲到/
    假早退。本函式讓有打卡的一側一律以班別基準計算。

    回傳 (is_late, late_minutes, is_early_leave, early_leave_minutes, status)。
    status 格式與 upload 既有預設路徑一致（late / early_leave / late+early_leave /
    normal，再依缺卡補 missing / +missing_in / +missing_out）。
    """
    is_late = bool(punch_in_dt is not None and punch_in_dt > shift_start_dt)
    late_minutes = (
        max(0, int((punch_in_dt - shift_start_dt).total_seconds() / 60))
        if is_late
        else 0
    )
    # 早退需 punch_out 落在班別窗內（shift_start ≤ punch_out < shift_end）。
    # qa-loop round2（2026-06-29）：跨夜班 shift_end 正規化到隔日 06:00，但只補下班卡時
    # punch_out 留在「當日」06:00（+1 日修正要求兩卡齊全），該時間戳早於 shift_start（當日
    # 22:00）→ 是未正規化的壞時間戳，舊式僅 `punch_out < shift_end` 會算成早退 1440 分 →
    # 扣整日薪。加 `punch_out >= shift_start` 下界即排除此壞值；同日漏上班卡的合法 lone
    # punch_out（如晚班 13:00-22:00、21:00 走）punch_out 仍 ≥ shift_start，早退 60 分照常偵測。
    is_early_leave = bool(
        punch_out_dt is not None and shift_start_dt <= punch_out_dt < shift_end_dt
    )
    early_leave_minutes = (
        max(0, int((shift_end_dt - punch_out_dt).total_seconds() / 60))
        if is_early_leave
        else 0
    )

    if is_late and is_early_leave:
        status = "late+early_leave"
    elif is_late:
        status = "late"
    elif is_early_leave:
        status = "early_leave"
    else:
        status = "normal"

    if punch_in_dt is None:
        status = "missing" if status == "normal" else status + "+missing_in"
    if punch_out_dt is None:
        status = "missing" if status == "normal" else status + "+missing_out"

    return is_late, late_minutes, is_early_leave, early_leave_minutes, status
