"""
Attendance - summary, anomaly report, and calendar endpoints
"""

import calendar as cal_module
import io
import logging
from calendar import monthrange
from datetime import date
from utils.taipei_time import today_taipei
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy import and_, case, func, or_

from models.approval import ApprovalStatus
from models.database import (
    get_session,
    Employee,
    Attendance,
    LeaveRecord,
    OvertimeRecord,
)
from utils.auth import require_staff_permission
from utils.error_messages import EMPLOYEE_DOES_NOT_EXIST
from utils.excel_utils import SafeWorksheet
from utils.permissions import Permission
from ._shared import LEAVE_TYPE_LABELS

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/today")
def get_today_attendance_summary(
    current_user: dict = Depends(require_staff_permission(Permission.ATTENDANCE_READ)),
):
    """取得今日出勤即時狀態"""
    session = get_session()
    try:
        today = today_taipei()

        total_employees = (
            session.query(Employee).filter(Employee.is_active == True).count()
        )

        # SQL aggregate 取代 Python 逐行計算
        today_counts = (
            session.query(
                # R4-8：出勤 = 至少有一個真實打卡的列；雙缺卡（無任何打卡）不算出勤，
                # 否則只有缺卡列的員工會虛增 present、虛減 absent。
                func.sum(
                    case(
                        (
                            and_(
                                Attendance.is_missing_punch_in == True,
                                Attendance.is_missing_punch_out == True,
                            ),
                            0,
                        ),
                        else_=1,
                    )
                ).label("present"),
                func.sum(case((Attendance.is_late == True, 1), else_=0)).label("late"),
                func.sum(
                    case(
                        (
                            or_(
                                Attendance.is_missing_punch_in == True,
                                Attendance.is_missing_punch_out == True,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("missing"),
            )
            .filter(Attendance.attendance_date == today)
            .first()
        )

        present_count = int(today_counts.present or 0)
        late_count = int(today_counts.late or 0)
        missing_count = int(today_counts.missing or 0)

        return {
            "date": today.isoformat(),
            "total_employees": total_employees,
            "present_count": present_count,
            "absent_count": max(0, total_employees - present_count),
            "late_count": late_count,
            "missing_count": missing_count,
        }
    finally:
        session.close()


@router.get("/summary")
def get_attendance_summary(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    current_user: dict = Depends(require_staff_permission(Permission.ATTENDANCE_READ)),
):
    """取得考勤統計摘要"""
    session = get_session()
    try:
        start_date = date(year, month, 1)
        _, last_day = monthrange(year, month)
        end_date = date(year, month, last_day)

        # SQL GROUP BY 取代 Python 端逐行累加，避免把整月打卡記錄載入記憶體
        rows = (
            session.query(
                Attendance.employee_id,
                func.count(Attendance.id).label("total_days"),
                func.sum(case((Attendance.status == "normal", 1), else_=0)).label(
                    "normal_days"
                ),
                func.sum(case((Attendance.is_late == True, 1), else_=0)).label(
                    "late_count"
                ),
                func.sum(case((Attendance.is_early_leave == True, 1), else_=0)).label(
                    "early_leave_count"
                ),
                func.sum(
                    case((Attendance.is_missing_punch_in == True, 1), else_=0)
                ).label("missing_punch_in"),
                func.sum(
                    case((Attendance.is_missing_punch_out == True, 1), else_=0)
                ).label("missing_punch_out"),
                func.coalesce(func.sum(Attendance.late_minutes), 0).label(
                    "total_late_minutes"
                ),
                func.coalesce(func.sum(Attendance.early_leave_minutes), 0).label(
                    "total_early_minutes"
                ),
            )
            .filter(
                Attendance.attendance_date >= start_date,
                Attendance.attendance_date <= end_date,
            )
            .group_by(Attendance.employee_id)
            .all()
        )

        # 只需要在職員工的名稱對照（離職員工自動排除）
        emp_map = {
            e.id: e
            for e in session.query(Employee).filter(Employee.is_active == True).all()
        }

        result = []
        for row in rows:
            emp = emp_map.get(row.employee_id)
            if not emp:
                continue
            result.append(
                {
                    "employee_id": emp.id,
                    "employee_name": emp.name,
                    "employee_number": emp.employee_id,
                    "total_days": row.total_days,
                    "normal_days": row.normal_days,
                    "late_count": row.late_count,
                    "early_leave_count": row.early_leave_count,
                    "missing_punch_in": row.missing_punch_in,
                    "missing_punch_out": row.missing_punch_out,
                    "total_late_minutes": row.total_late_minutes,
                    "total_early_minutes": row.total_early_minutes,
                }
            )

        return result
    finally:
        session.close()


@router.get("/today-anomalies")
def get_today_anomalies(
    current_user: dict = Depends(require_staff_permission(Permission.ATTENDANCE_READ)),
):
    """今日打卡異常員工清單"""
    session = get_session()
    try:
        today = today_taipei()

        employees = session.query(Employee).filter(Employee.is_active == True).all()

        today_records = (
            session.query(Attendance).filter(Attendance.attendance_date == today).all()
        )
        att_map = {r.employee_id: r for r in today_records}

        anomalies = []
        for emp in employees:
            att = att_map.get(emp.id)
            if att is None:
                anomalies.append(
                    {
                        "employee_id": emp.employee_id,
                        "employee_name": emp.name,
                        "anomaly_type": "absent",
                        "late_minutes": None,
                    }
                )
            else:
                if att.is_late:
                    anomalies.append(
                        {
                            "employee_id": emp.employee_id,
                            "employee_name": emp.name,
                            "anomaly_type": "late",
                            "late_minutes": att.late_minutes,
                        }
                    )
                if att.is_missing_punch_in or att.is_missing_punch_out:
                    anomalies.append(
                        {
                            "employee_id": emp.employee_id,
                            "employee_name": emp.name,
                            "anomaly_type": "missing_punch",
                            "late_minutes": None,
                        }
                    )

        return {
            "date": today.isoformat(),
            "anomalies": anomalies,
        }
    finally:
        session.close()


def _build_anomaly_report_workbook(rows) -> Workbook:
    """由 (Attendance, Employee) tuple 序列建出考勤異常清單 Workbook。

    透過 SafeWorksheet 包裝寫入，員工姓名 / 狀態等可編輯欄位即使含 Excel 公式
    前綴（=、+、-、@、|）也不會在主管 / HR 端開啟時被當公式執行（SEC-005）。
    """
    wb = Workbook()
    ws = SafeWorksheet(wb.active)
    ws.title = "考勤異常清單"
    ws.append(
        [
            "員工姓名",
            "日期",
            "上班打卡",
            "下班打卡",
            "狀態",
            "遲到分鐘",
            "早退分鐘",
        ]
    )

    def _fmt_time(t):
        if t is None:
            return "未打卡"
        try:
            return t.strftime("%H:%M")
        except Exception:
            return str(t)

    for att, emp in rows:
        states = []
        if att.is_late:
            states.append("遲到")
        if att.is_early_leave:
            states.append("早退")
        if att.is_missing_punch_in:
            states.append("缺打卡(上班)")
        if att.is_missing_punch_out:
            states.append("缺打卡(下班)")
        ws.append(
            [
                emp.name,
                att.attendance_date.isoformat(),
                _fmt_time(att.punch_in_time),
                _fmt_time(att.punch_out_time),
                "/".join(states) or att.status or "",
                int(att.late_minutes or 0),
                int(att.early_leave_minutes or 0),
            ]
        )

    return wb


@router.get("/anomaly-report")
def download_anomaly_report(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    current_user: dict = Depends(require_staff_permission(Permission.ATTENDANCE_READ)),
):
    """下載指定月份異常清單（即時從 Attendance DB 重新產生）。

    舊實作直接吐 output/anomaly_report.xlsx，這份檔案由上一次匯入覆寫，
    任何 ATTENDANCE_READ 使用者都會拿到他人剛匯入的內容、或拿到月初的舊檔。
    改成依 (year, month) 即時計算，請求隔離、無共享狀態。
    """
    _, last_day = monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)

    session = get_session()
    try:
        rows = (
            session.query(Attendance, Employee)
            .join(Employee, Attendance.employee_id == Employee.id)
            .filter(
                Attendance.attendance_date >= start,
                Attendance.attendance_date <= end,
                or_(
                    Attendance.is_late == True,  # noqa: E712
                    Attendance.is_early_leave == True,  # noqa: E712
                    Attendance.is_missing_punch_in == True,  # noqa: E712
                    Attendance.is_missing_punch_out == True,  # noqa: E712
                ),
            )
            .order_by(Attendance.attendance_date, Employee.name)
            .limit(5000)
            .all()
        )
    finally:
        session.close()

    wb = _build_anomaly_report_workbook(rows)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"考勤異常清單_{year}_{month:02d}.xlsx"
    return StreamingResponse(
        buf,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )


def _get_attendance_calendar_legacy(
    session,
    emp,
    employee_id: int,
    year: int,
    month: int,
):
    """LEGACY: Task 27 之前的 leave_map join 邏輯。

    保留供 Task 28 parity test 使用，merge 後一週 follow-up 刪除。
    """
    _, last_day = cal_module.monthrange(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)

    attendances = (
        session.query(Attendance)
        .filter(
            Attendance.employee_id == employee_id,
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= end_date,
        )
        .all()
    )
    att_map = {a.attendance_date: a for a in attendances}

    leaves = (
        session.query(LeaveRecord)
        .filter(
            LeaveRecord.employee_id == employee_id,
            LeaveRecord.start_date <= end_date,
            LeaveRecord.end_date >= start_date,
            LeaveRecord.status == ApprovalStatus.APPROVED.value,
        )
        .all()
    )

    leave_map = {}
    for lv in leaves:
        d = max(lv.start_date, start_date)
        while d <= min(lv.end_date, end_date):
            leave_map[d] = lv
            d = date.fromordinal(d.toordinal() + 1)

    overtimes = (
        session.query(OvertimeRecord)
        .filter(
            OvertimeRecord.employee_id == employee_id,
            OvertimeRecord.overtime_date >= start_date,
            OvertimeRecord.overtime_date <= end_date,
            OvertimeRecord.status == ApprovalStatus.APPROVED.value,
        )
        .all()
    )
    ot_map = {o.overtime_date: o for o in overtimes}

    days = []
    work_days = 0
    late_count = 0
    leave_days = 0
    overtime_hours = 0

    for day_num in range(1, last_day + 1):
        d = date(year, month, day_num)
        att = att_map.get(d)
        lv = leave_map.get(d)
        ot = ot_map.get(d)

        day_data = {
            "date": d.isoformat(),
            "weekday": d.weekday(),
            "punch_in": (
                att.punch_in_time.strftime("%H:%M")
                if att and att.punch_in_time
                else None
            ),
            "punch_out": (
                att.punch_out_time.strftime("%H:%M")
                if att and att.punch_out_time
                else None
            ),
            "status": att.status if att else None,
            "is_late": att.is_late if att else False,
            "late_minutes": att.late_minutes if att else 0,
            "is_early_leave": att.is_early_leave if att else False,
            "leave_type": lv.leave_type if lv else None,
            "leave_type_label": (LEAVE_TYPE_LABELS.get(lv.leave_type) if lv else None),
            "leave_hours": lv.leave_hours if lv else 0,
            "overtime_hours": ot.hours if ot else 0,
            "overtime_type": ot.overtime_type if ot else None,
            "remark": att.remark if att else None,
        }
        days.append(day_data)

        if att:
            work_days += 1
            if att.is_late:
                late_count += 1
        if lv:
            leave_days += lv.leave_hours / 8
        if ot:
            overtime_hours += ot.hours

    return {
        "employee_name": emp.name,
        "employee_id": emp.employee_id,
        "year": year,
        "month": month,
        "days": days,
        "summary": {
            "work_days": work_days,
            "late_count": late_count,
            "leave_days": round(leave_days, 1),
            "overtime_hours": round(overtime_hours, 1),
        },
    }


def _get_attendance_calendar_new(
    session,
    emp,
    employee_id: int,
    year: int,
    month: int,
) -> dict:
    """新版月出勤日曆核心邏輯。

    使用 Attendance outerjoin LeaveRecord（透過 leave_record_id）取代舊版
    leave_map 字典 + while 迴圈補丁，以 AttendanceRecord 作為出勤唯一 SoT。
    供 router endpoint 與 parity test 共用，不依賴 get_session()。
    """
    _, last_day = cal_module.monthrange(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)

    # 單一 outerjoin query 取代兩次 query + leave_map while 迴圈
    rows = (
        session.query(Attendance, LeaveRecord)
        .outerjoin(
            LeaveRecord,
            Attendance.leave_record_id == LeaveRecord.id,
        )
        .filter(
            Attendance.employee_id == employee_id,
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= end_date,
        )
        .all()
    )
    att_lv_map = {att.attendance_date: (att, lv) for att, lv in rows}

    overtimes = (
        session.query(OvertimeRecord)
        .filter(
            OvertimeRecord.employee_id == employee_id,
            OvertimeRecord.overtime_date >= start_date,
            OvertimeRecord.overtime_date <= end_date,
            OvertimeRecord.status == ApprovalStatus.APPROVED.value,
        )
        .all()
    )
    ot_map = {o.overtime_date: o for o in overtimes}

    days = []
    work_days = 0
    late_count = 0
    leave_days = 0
    overtime_hours = 0

    for day_num in range(1, last_day + 1):
        d = date(year, month, day_num)
        att, lv = att_lv_map.get(d, (None, None))
        ot = ot_map.get(d)

        day_data = {
            "date": d.isoformat(),
            "weekday": d.weekday(),
            "punch_in": (
                att.punch_in_time.strftime("%H:%M")
                if att and att.punch_in_time
                else None
            ),
            "punch_out": (
                att.punch_out_time.strftime("%H:%M")
                if att and att.punch_out_time
                else None
            ),
            "status": att.status if att else None,
            "is_late": att.is_late if att else False,
            "late_minutes": att.late_minutes if att else 0,
            "is_early_leave": att.is_early_leave if att else False,
            "leave_type": lv.leave_type if lv else None,
            "leave_type_label": (LEAVE_TYPE_LABELS.get(lv.leave_type) if lv else None),
            "leave_hours": lv.leave_hours if lv else 0,
            "overtime_hours": ot.hours if ot else 0,
            "overtime_type": ot.overtime_type if ot else None,
            "remark": att.remark if att else None,
        }
        days.append(day_data)

        if att:
            work_days += 1
            if att.is_late:
                late_count += 1
        if lv:
            leave_days += lv.leave_hours / 8
        if ot:
            overtime_hours += ot.hours

    return {
        "employee_name": emp.name,
        "employee_id": emp.employee_id,
        "year": year,
        "month": month,
        "days": days,
        "summary": {
            "work_days": work_days,
            "late_count": late_count,
            "leave_days": round(leave_days, 1),
            "overtime_hours": round(overtime_hours, 1),
        },
    }


@router.get("/calendar")
def get_attendance_calendar(
    employee_id: int = Query(...),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    current_user: dict = Depends(require_staff_permission(Permission.ATTENDANCE_READ)),
):
    """取得員工月出勤日曆資料。

    核心邏輯委由 _get_attendance_calendar_new() 實作，此 endpoint 僅負責
    驗證員工存在並管理 session 生命週期。
    """
    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail=EMPLOYEE_DOES_NOT_EXIST)
        return _get_attendance_calendar_new(session, emp, employee_id, year, month)
    finally:
        session.close()
