"""寫 Attendance 前合併當日有效 leave 資訊。

設計理念:寫入端負責 leave-awareness。不依靠 leave 端 trigger reapply 的隱性合約。

對等於 sync service:sync 在 leaves 生命週期事件寫 attendance;
merge 在 attendance 寫入事件 pull leave。兩者並列、不互呼。
"""

from datetime import time
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from models.attendance import Attendance, AttendanceStatus
from models.leave import LeaveRecord
from models.approval import ApprovalStatus
from utils.attendance_calc import (
    compute_early_leave_minutes_with_leave,
    compute_late_minutes_with_leave,
    sync_attendance_flags,
)

# 預設排班（對齊 services/employee_leave_attendance_sync）
DEFAULT_SCHEDULED_START = time(9, 0)
DEFAULT_SCHEDULED_END = time(18, 0)


def merge_attendance_with_leave(att: Attendance, session: Session) -> None:
    """In-place 把當日有效 leave 的 leave_record_id / partial_leave_hours /
    late_minutes 等欄合進 att。純函式,只讀 session,不把 att 加入 session。

    決策表:
      1. 無 leave            → 清 leave_*,保留 caller 算好的 status/late_minutes
      2. 全天 + 無打卡       → status=LEAVE,清打卡,leave_record_id 寫入
      3. 全天 + 有打卡       → 保留打卡,leave_record_id 寫入,partial_leave_hours=0
      4. 部分 + 有打卡       → 保留打卡,partial_leave_hours 寫入,late 重算
      5. 部分 + 無打卡       → status=ABSENT,leave_record_id + partial_leave_hours 寫入
      6. 同日多筆 leave      → 取最早 id
    """
    leaves = (
        session.query(LeaveRecord)
        .filter(
            LeaveRecord.employee_id == att.employee_id,
            LeaveRecord.start_date <= att.attendance_date,
            LeaveRecord.end_date >= att.attendance_date,
            LeaveRecord.status == ApprovalStatus.APPROVED.value,
        )
        .order_by(LeaveRecord.id)
        .all()
    )

    if not leaves:
        # case 1:無 leave → 清 leave_*,保留 caller 算好的欄位
        att.leave_record_id = None
        att.partial_leave_hours = None
        return

    leave = leaves[0]  # case 6:同日多筆取最早 id
    att.leave_record_id = leave.id

    if _is_full_day(leave):
        if att.punch_in_time is None and att.punch_out_time is None:
            # case 2:全天 + 無打卡
            att.status = AttendanceStatus.LEAVE.value
            att.partial_leave_hours = None
            att.late_minutes = 0
            att.early_leave_minutes = 0
        else:
            # case 3:全天請假但人來了（有打卡）
            att.partial_leave_hours = Decimal("0")
            # status / late_minutes 保留 caller 算好的
    else:
        # 部分請假（半天/小時）
        att.partial_leave_hours = Decimal(str(leave.leave_hours))
        lv_start = _parse_hhmm(leave.start_time)
        lv_end = _parse_hhmm(leave.end_time)

        if att.punch_in_time is None and att.punch_out_time is None:
            # case 5:部分 + 無打卡
            att.status = AttendanceStatus.ABSENT.value
            att.late_minutes = 0
            att.early_leave_minutes = 0
        else:
            # case 4:部分 + 有打卡 → leave-aware 重算
            sched_start, sched_end = _get_employee_schedule(session, att.employee_id)

            if att.punch_in_time is not None:
                # punch_in_time 是 DateTime，需先轉成 time
                punch_in_t = (
                    att.punch_in_time.time()
                    if hasattr(att.punch_in_time, "time")
                    else att.punch_in_time
                )
                att.late_minutes = compute_late_minutes_with_leave(
                    punch_in=punch_in_t,
                    scheduled_start=sched_start,
                    leave_start=lv_start,
                    leave_end=lv_end,
                )

            if att.punch_out_time is not None:
                # 跨夜班：下班打卡落在隔日（caller 已 normalize 為 +1 天），
                # 不可用 .time() 截斷日期後比對當日 scheduled_end（會誤算 ~960 分早退，P1-4）。
                if (
                    hasattr(att.punch_out_time, "date")
                    and att.punch_out_time.date() > att.attendance_date
                ):
                    att.early_leave_minutes = 0
                else:
                    # punch_out_time 是 DateTime，需先轉成 time
                    punch_out_t = (
                        att.punch_out_time.time()
                        if hasattr(att.punch_out_time, "time")
                        else att.punch_out_time
                    )
                    att.early_leave_minutes = compute_early_leave_minutes_with_leave(
                        punch_out=punch_out_t,
                        scheduled_end=sched_end,
                        leave_start=lv_start,
                        leave_end=lv_end,
                    )

            # status 修復:late/early_leave 都歸零 → 從 LATE/EARLY_LEAVE 退回 NORMAL
            late_min = att.late_minutes or 0
            early_min = att.early_leave_minutes or 0
            if late_min == 0 and early_min == 0:
                if att.status in (
                    AttendanceStatus.LATE.value,
                    AttendanceStatus.EARLY_LEAVE.value,
                ):
                    att.status = AttendanceStatus.NORMAL.value

    # P0-3：leave 併入改動了 status/minutes/punch，重新同步布林旗標
    # （case 1 無 leave 已於前面 return，保留 caller recompute 的旗標）。
    sync_attendance_flags(att)


def _is_full_day(leave: LeaveRecord) -> bool:
    """全天 = start_time/end_time 都 NULL 且 leave_hours 是 None 或 >= 8。"""
    return (
        leave.start_time is None
        and leave.end_time is None
        and (leave.leave_hours is None or leave.leave_hours >= 8)
    )


def _parse_hhmm(s: Optional[str]) -> Optional[time]:
    """解析 "HH:MM" 字串成 time，None 回傳 None。"""
    if s is None:
        return None
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


def _get_employee_schedule(session: Session, employee_id: int) -> tuple[time, time]:
    """對齊 services/employee_leave_attendance_sync._get_employee_schedule。

    日後員工 model 加排班欄，兩處同步改。
    """
    return DEFAULT_SCHEDULED_START, DEFAULT_SCHEDULED_END
