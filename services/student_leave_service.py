"""services/student_leave_service.py — 學生請假商業邏輯（純函式 + DB 操作）

關鍵純函式 compute_attendance_dates：把「家長申請的請假期間」轉成「學生
應到日清單」（後續 approve 時用來 upsert StudentAttendance）。

純函式不依賴 DB session，方便單元測試覆蓋週末/假日/補班混合情境
（CLAUDE.md「純商業邏輯必須有單元測試」）。

審核 approve 時的 attendance 寫入規則（plan A.4）：
- 對 compute_attendance_dates 回傳的每一個應到日 upsert StudentAttendance
- 若該日無紀錄 → 建立 status=leave_type, remark=REMARK_PREFIX|leave_id
- 若該日已有紀錄 → 覆蓋 status，remark 改為 REMARK_PREFIX|leave_id（保留
  recorded_by 不變）
- reject / cancel 時走 revert_approved_dates，僅清除 remark 前綴吻合者
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from services.workday_rules import classify_day

REMARK_PREFIX = "家長申請#"


def compute_attendance_dates(
    start_date: date,
    end_date: date,
    holiday_map: dict[date, str],
    makeup_map: dict[date, str],
) -> list[date]:
    """回傳區間內「學生應到日」的清單（升冪、含起迄日）。

    應到日規則：
    - 補班日（makeup）→ 應到（即使是週六）
    - 國定假日 → 不到
    - 一般週末 → 不到
    - 一般工作日 → 應到

    Raises:
        ValueError：start_date > end_date
    """
    if start_date > end_date:
        raise ValueError("start_date 不可晚於 end_date")
    out: list[date] = []
    cur = start_date
    while cur <= end_date:
        info = classify_day(cur, holiday_map, makeup_map)
        # is_makeup_workday=True → kind=workday；is_holiday=True → kind=holiday；
        # weekend → kind=weekend。應到 = kind=='workday'。
        if info["kind"] == "workday":
            out.append(cur)
        cur += timedelta(days=1)
    return out


def make_remark(leave_id: int) -> str:
    """生成 attendance.remark 內容（含可解析的前綴與 leave_id）。"""
    return f"{REMARK_PREFIX}{leave_id}"


def is_remark_owned_by_leave(remark: str | None, leave_id: int) -> bool:
    """判斷某筆 attendance 的 remark 是否屬於這次請假審核所寫。"""
    if not remark:
        return False
    return remark.strip() == make_remark(leave_id)


from typing import Optional  # noqa: E402

from models.database import StudentAttendance, StudentLeaveRequest  # noqa: E402
from services.workday_rules import load_day_rule_maps  # noqa: E402


def apply_attendance_for_leave(
    session,
    leave: StudentLeaveRequest,
    recorded_by: Optional[int] = None,
) -> int:
    """在當前 session（caller 開的 transaction）upsert StudentAttendance。

    對 compute_attendance_dates 回傳的每個應到日：
    - 若該日無紀錄 → 建立 status=leave_type, remark=家長申請#<id>, recorded_by=傳入值（預設 None）
    - 若該日已有紀錄 → 覆蓋 status / remark；保留原 recorded_by 不變
    回傳實際被建立或覆蓋的天數。
    """
    holiday_map, makeup_map = load_day_rule_maps(
        session, leave.start_date, leave.end_date
    )
    dates = compute_attendance_dates(
        leave.start_date, leave.end_date, holiday_map, makeup_map
    )
    new_remark = make_remark(leave.id)
    affected = 0
    for d in dates:
        existing = (
            session.query(StudentAttendance)
            .filter(
                StudentAttendance.student_id == leave.student_id,
                StudentAttendance.date == d,
            )
            .first()
        )
        if existing is None:
            session.add(
                StudentAttendance(
                    student_id=leave.student_id,
                    date=d,
                    status=leave.leave_type,
                    remark=new_remark,
                    recorded_by=recorded_by,
                )
            )
        else:
            existing.status = leave.leave_type
            existing.remark = new_remark
            # recorded_by 不覆蓋（保留原作者）
        affected += 1
    return affected


def revert_attendance_for_leave(session, leave: StudentLeaveRequest) -> int:
    """反向清除：僅刪除 remark 吻合的紀錄（保留教師後手紀錄）。"""
    rows = (
        session.query(StudentAttendance)
        .filter(
            StudentAttendance.student_id == leave.student_id,
            StudentAttendance.date >= leave.start_date,
            StudentAttendance.date <= leave.end_date,
        )
        .all()
    )
    affected = 0
    for r in rows:
        if is_remark_owned_by_leave(r.remark, leave.id):
            session.delete(r)
            affected += 1
    return affected
