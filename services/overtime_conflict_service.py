"""services/overtime_conflict_service.py — 加班衝突檢查 (F1 第三波)。

從 api/overtimes.py 抽出四條 helper：
- check_employee_has_conflicting_leave(session, ...) — 與 leave 同日同時段衝突
- check_overtime_overlap(session, ...) — 與自己其他加班申請時段重疊
- check_overtime_type_calendar(session, ...) — overtime_type 與國定假日對齊
- check_monthly_overtime_cap(session, ...) — 月度 46h 上限（勞基法第 32 條）

供 admin（api/overtimes.py）與 portal（api/portal/overtimes.py）兩端共用，
封掉 portal handler 內 lazy `from api.overtimes import _check_*` 的反向耦合。

本檔僅含 DB 查詢 + 純判斷邏輯；不持有任何全域狀態。session 由呼叫端注入。
"""

import calendar as cal_module
from datetime import date
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_, func, or_

from models.database import Holiday, LeaveRecord, OvertimeRecord
from utils.constants import (
    MAX_MONTHLY_OVERTIME_HOURS,
    MAX_QUARTERLY_OVERTIME_HOURS,
    OVERTIME_QUARTERLY_WINDOW_MONTHS,
)

# -- 純函式（無 session 依賴）：抽出便於 unit test ----------------------


def _assert_within_monthly_cap(
    existing_hours: float, new_hours: float, year: int, month: int
) -> None:
    """純函式：驗證既存 + 新加班時數不超過勞基法第 32 條第 2 項 46h/月上限。"""
    existing = float(existing_hours or 0)
    new = float(new_hours or 0)
    total = existing + new
    if total > MAX_MONTHLY_OVERTIME_HOURS + 1e-9:
        raise HTTPException(
            status_code=400,
            detail=(
                f"該員工 {year}/{month} 已申請加班 {existing:.1f} 小時，"
                f"加上此筆 {new:.1f} 小時合計 {total:.1f} 小時，"
                f"超過勞基法第 32 條每月延長工時上限 {MAX_MONTHLY_OVERTIME_HOURS:.0f} 小時。"
            ),
        )


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    """月份位移 helper：(2026, 5) + 2 = (2026, 7)；(2026, 2) - 3 = (2025, 11)。

    Python 的 // 與 % 對負數做 floor division wrap，正好對應曆月跨年語意。
    """
    total = (year * 12 + month - 1) + offset
    return total // 12, total % 12 + 1


def _assert_within_quarterly_cap(
    existing_hours: float,
    new_hours: float,
    window_label: str,
    employee_id: int,
) -> None:
    """純函式：驗證單一窗口既存 + 新加班時數不超過勞基法第 32 條第 2 項
    每連續三個月 138h 上限。

    Caller (`check_quarterly_overtime_cap`) 對 W1/W2/W3 三個 rolling 3-month 窗口
    依序呼叫此函式 — 第一個違反的窗口即 raise，訊息中的 window_label 標明該窗口。

    訊息含 6 要素：員工 ID、窗口、累計、新筆、合計、上限 + 法源。
    """
    existing = float(existing_hours or 0)
    new = float(new_hours or 0)
    total = existing + new
    if total > MAX_QUARTERLY_OVERTIME_HOURS + 1e-9:
        raise HTTPException(
            status_code=400,
            detail=(
                f"員工 #{employee_id} 連續三個月（{window_label}）"
                f"已申請加班 {existing:.1f} 小時，"
                f"加上此筆 {new:.1f} 小時合計 {total:.1f} 小時，"
                f"超過勞基法第 32 條第 2 項每連續三個月延長工時上限 "
                f"{MAX_QUARTERLY_OVERTIME_HOURS:.0f} 小時。"
            ),
        )


def _validate_overtime_type_matches_calendar(
    overtime_type: str, is_statutory_holiday: bool
) -> None:
    """純函式：overtime_type 與該日是否為國定假日需一致（勞基法第 37 條）。

    - "holiday" 但日期非國定假日 → 400（防止溢付）
    - "weekday"/"weekend" 但日期為國定假日 → 400（防止短付違反第 37 條）
    """
    if overtime_type == "holiday" and not is_statutory_holiday:
        raise HTTPException(
            status_code=400,
            detail="該日期不在國定假日清單，請改用 weekday 或 weekend",
        )
    if overtime_type in ("weekday", "weekend") and is_statutory_holiday:
        raise HTTPException(
            status_code=400,
            detail=(
                "該日期為國定假日，加班類型須為 holiday 以加倍發給工資"
                "（勞基法第 37 條）"
            ),
        )


# -- DB-aware 檢查 -----------------------------------------------------


def check_employee_has_conflicting_leave(
    session,
    employee_id: int,
    overtime_date: date,
    start_time,  # datetime | None
    end_time,  # datetime | None
) -> None:
    """申請加班時檢查同員工同時段是否已有 approved/pending 請假。

    修補 2026-05-11 P1-5：請假與加班不互查重疊，導致同日扣款 + 加班費雙重溢付。

    時段比對規則：
    - OT 全日（start_time/end_time 為 None）→ 與 leave 同日就衝突
    - OT 半日 → 與 leave 時段比對；leave 缺時段視為全日衝突

    NOTE: 目前只在 create 路徑使用。若未來在 update 路徑也呼叫此 helper，需新增
    exclude_leave_id 參數避免自我衝突；同步調整 check_employee_has_conflicting_overtime。
    """
    candidates = (
        session.query(LeaveRecord)
        .filter(
            LeaveRecord.employee_id == employee_id,
            LeaveRecord.is_approved.in_([None, True]),
            LeaveRecord.start_date <= overtime_date,
            LeaveRecord.end_date >= overtime_date,
        )
        .all()
    )
    for lv in candidates:
        # OT 全日 → 同日有 leave 即衝突
        if start_time is None or end_time is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"員工於 {overtime_date} 已有請假申請 #{lv.id}"
                    f"（{lv.leave_type}），加班時段與請假重疊"
                ),
            )
        # leave 全日 → 衝突
        if not lv.start_time or not lv.end_time:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"員工於 {overtime_date} 已有全日請假 #{lv.id}"
                    f"（{lv.leave_type}），加班時段與請假重疊"
                ),
            )
        # 半日 vs 半日：時段精比
        ot_start_str = start_time.strftime("%H:%M")
        ot_end_str = end_time.strftime("%H:%M")
        if max(ot_start_str, lv.start_time) < min(ot_end_str, lv.end_time):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"員工於 {overtime_date} 已有請假 #{lv.id}"
                    f"（{lv.start_time}~{lv.end_time}），加班時段與其重疊"
                ),
            )


def check_overtime_overlap(
    session,
    employee_id: int,
    overtime_date: date,
    start_time,
    end_time,
    exclude_id: Optional[int] = None,
) -> Optional["OvertimeRecord"]:
    """檢查員工在指定日期是否已有時間重疊的加班申請（待審核或已核准）。

    重疊規則：
    - 已駁回的申請不列入，允許重新申請
    - 若新申請或現有記錄缺少時間資訊，同日即視為重疊
    - 若雙方都有 start/end time，做時間區間重疊判斷（start1 < end2 AND start2 < end1）
    """
    q = session.query(OvertimeRecord).filter(
        OvertimeRecord.employee_id == employee_id,
        OvertimeRecord.overtime_date == overtime_date,
        or_(OvertimeRecord.is_approved.is_(None), OvertimeRecord.is_approved == True),
    )
    if exclude_id is not None:
        q = q.filter(OvertimeRecord.id != exclude_id)

    # 若新申請缺少時間，同日任何記錄均視為重疊（維持原邏輯）
    if start_time is None or end_time is None:
        return q.first()

    # 有明確時間：DB 端排除「確定不重疊」的記錄
    # 保留：既有記錄缺少時間（無法比對，視為重疊），或時間區間重疊
    q = q.filter(
        or_(
            OvertimeRecord.start_time.is_(None),
            OvertimeRecord.end_time.is_(None),
            and_(
                OvertimeRecord.start_time < end_time,
                OvertimeRecord.end_time > start_time,
            ),
        )
    )
    return q.first()


def check_overtime_type_calendar(
    session, target_date: date, overtime_type: str
) -> None:
    """查詢 Holiday 表後呼叫純函式驗證 overtime_type 對齊國定假日。"""
    is_holiday = (
        session.query(Holiday)
        .filter(Holiday.date == target_date, Holiday.is_active == True)
        .first()
        is not None
    )
    _validate_overtime_type_matches_calendar(overtime_type, is_holiday)


def check_monthly_overtime_cap(
    session,
    employee_id: int,
    target_date: date,
    new_hours: float,
    exclude_id: Optional[int] = None,
) -> None:
    """查詢員工指定月份已申請（待審+已核准）加班時數，加上新時數後驗證不超過月上限。

    已駁回的申請不計入（釋放時數額度）。
    """
    year, month = target_date.year, target_date.month
    _, last_day = cal_module.monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)
    q = session.query(func.coalesce(func.sum(OvertimeRecord.hours), 0)).filter(
        OvertimeRecord.employee_id == employee_id,
        OvertimeRecord.overtime_date >= start,
        OvertimeRecord.overtime_date <= end,
        or_(
            OvertimeRecord.is_approved.is_(None),
            OvertimeRecord.is_approved == True,
        ),
    )
    if exclude_id is not None:
        q = q.filter(OvertimeRecord.id != exclude_id)
    existing = float(q.scalar() or 0)
    _assert_within_monthly_cap(existing, new_hours, year, month)


def check_quarterly_overtime_cap(
    session,
    employee_id: int,
    target_date: date,
    new_hours: float,
    exclude_id: Optional[int] = None,
) -> None:
    """查詢員工 3 個包含 target_date 月份的 rolling 3-month 窗口已申請 OT，
    加上新時數後驗證任一窗口不超過 138h（勞基法第 32 條第 2 項）。

    窗口定義（M = target_date.month）：
    - W1: [M-2, M]
    - W2: [M-1, M+1]
    - W3: [M, M+2]

    已駁回的申請不計入；exclude_id 用於 update 路徑排除自身舊紀錄。
    多窗口同時超標時回報「最先超過」（W1→W2→W3 順序），讓 HR 從早到晚排查。
    """
    windows: list[tuple[date, date, str]] = []
    n = OVERTIME_QUARTERLY_WINDOW_MONTHS  # = 3
    for offset in range(-(n - 1), 1):  # (-2, -1, 0)
        start_year, start_month = _shift_month(
            target_date.year, target_date.month, offset
        )
        end_year, end_month = _shift_month(
            target_date.year, target_date.month, offset + n - 1  # offset + 2
        )
        start = date(start_year, start_month, 1)
        _, last_day = cal_module.monthrange(end_year, end_month)
        end = date(end_year, end_month, last_day)
        label = f"{start_year}/{start_month:02d}~{end_year}/{end_month:02d}"
        windows.append((start, end, label))

    for start, end, label in windows:
        q = session.query(func.coalesce(func.sum(OvertimeRecord.hours), 0)).filter(
            OvertimeRecord.employee_id == employee_id,
            OvertimeRecord.overtime_date >= start,
            OvertimeRecord.overtime_date <= end,
            or_(
                OvertimeRecord.is_approved.is_(None),
                OvertimeRecord.is_approved == True,
            ),
        )
        if exclude_id is not None:
            q = q.filter(OvertimeRecord.id != exclude_id)
        existing = float(q.scalar() or 0)
        _assert_within_quarterly_cap(existing, new_hours, label, employee_id)
