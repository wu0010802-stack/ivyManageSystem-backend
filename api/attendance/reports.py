"""
Attendance - summary, anomaly report, and calendar endpoints
"""

import calendar as cal_module
import logging
import os
from calendar import monthrange
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from models.database import get_session, Employee, Attendance, LeaveRecord, OvertimeRecord
from utils.auth import require_permission
from utils.permissions import Permission
from ._shared import LEAVE_TYPE_LABELS

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/today")
async def get_today_attendance_summary(
    current_user: dict = Depends(require_permission(Permission.ATTENDANCE_READ)),
):
    """取得今日出勤即時狀態"""
    session = get_session()
    try:
        today = date.today()

        total_employees = session.query(Employee).filter(Employee.is_active == True).count()

        today_records = session.query(Attendance).filter(
            Attendance.attendance_date == today
        ).all()

        present_count = len(today_records)
        late_count = sum(1 for a in today_records if a.is_late)
        missing_count = sum(
            1 for a in today_records
            if a.is_missing_punch_in or a.is_missing_punch_out
        )

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
async def get_attendance_summary(
    year: int = Query(...),
    month: int = Query(...),
    current_user: dict = Depends(require_permission(Permission.ATTENDANCE_READ)),
):
    """取得考勤統計摘要"""
    session = get_session()
    try:
        start_date = date(year, month, 1)
        _, last_day = monthrange(year, month)
        end_date = date(year, month, last_day)

        employees = session.query(Employee).filter(Employee.is_active == True).all()

        all_attendances = session.query(Attendance).filter(
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= end_date
        ).all()

        att_by_emp = {}
        for a in all_attendances:
            att_by_emp.setdefault(a.employee_id, []).append(a)

        result = []
        for emp in employees:
            attendances = att_by_emp.get(emp.id, [])

            if not attendances:
                continue

            total_days = len(attendances)
            normal_days = sum(1 for a in attendances if a.status == "normal")
            late_count = sum(1 for a in attendances if a.is_late)
            early_leave_count = sum(1 for a in attendances if a.is_early_leave)
            missing_punch_in = sum(1 for a in attendances if a.is_missing_punch_in)
            missing_punch_out = sum(1 for a in attendances if a.is_missing_punch_out)
            total_late_minutes = sum(a.late_minutes or 0 for a in attendances)
            total_early_minutes = sum(a.early_leave_minutes or 0 for a in attendances)

            result.append({
                "employee_id": emp.id,
                "employee_name": emp.name,
                "employee_number": emp.employee_id,
                "total_days": total_days,
                "normal_days": normal_days,
                "late_count": late_count,
                "early_leave_count": early_leave_count,
                "missing_punch_in": missing_punch_in,
                "missing_punch_out": missing_punch_out,
                "total_late_minutes": total_late_minutes,
                "total_early_minutes": total_early_minutes
            })

        return result
    finally:
        session.close()


@router.get("/today-anomalies")
async def get_today_anomalies(
    late_threshold: int = Query(15, ge=0, le=120),
    current_user: dict = Depends(require_permission(Permission.ATTENDANCE_READ)),
):
    """今日打卡異常員工清單"""
    session = get_session()
    try:
        today = date.today()

        employees = session.query(Employee).filter(Employee.is_active == True).all()

        today_records = session.query(Attendance).filter(
            Attendance.attendance_date == today
        ).all()
        att_map = {r.employee_id: r for r in today_records}

        anomalies = []
        for emp in employees:
            att = att_map.get(emp.id)
            if att is None:
                anomalies.append({
                    "employee_id": emp.employee_id,
                    "employee_name": emp.name,
                    "anomaly_type": "absent",
                    "late_minutes": None,
                })
            else:
                if att.is_late and (att.late_minutes or 0) > late_threshold:
                    anomalies.append({
                        "employee_id": emp.employee_id,
                        "employee_name": emp.name,
                        "anomaly_type": "late",
                        "late_minutes": att.late_minutes,
                    })
                if att.is_missing_punch_in or att.is_missing_punch_out:
                    anomalies.append({
                        "employee_id": emp.employee_id,
                        "employee_name": emp.name,
                        "anomaly_type": "missing_punch",
                        "late_minutes": None,
                    })

        return {
            "date": today.isoformat(),
            "late_threshold": late_threshold,
            "anomalies": anomalies,
        }
    finally:
        session.close()


@router.get("/anomaly-report")
async def download_anomaly_report(current_user: dict = Depends(require_permission(Permission.ATTENDANCE_READ))):
    """下載異常清單"""
    file_path = "output/anomaly_report.xlsx"
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="報表尚未產生")
    return FileResponse(file_path, filename="考勤異常清單.xlsx")


@router.get("/calendar")
def get_attendance_calendar(
    employee_id: int = Query(...),
    year: int = Query(...),
    month: int = Query(...),
    current_user: dict = Depends(require_permission(Permission.ATTENDANCE_READ)),
):
    """取得員工月出勤日曆資料"""
    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="員工不存在")

        _, last_day = cal_module.monthrange(year, month)
        start_date = date(year, month, 1)
        end_date = date(year, month, last_day)

        attendances = session.query(Attendance).filter(
            Attendance.employee_id == employee_id,
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= end_date
        ).all()
        att_map = {a.attendance_date: a for a in attendances}

        leaves = session.query(LeaveRecord).filter(
            LeaveRecord.employee_id == employee_id,
            LeaveRecord.start_date <= end_date,
            LeaveRecord.end_date >= start_date,
            LeaveRecord.is_approved == True
        ).all()

        leave_map = {}
        for lv in leaves:
            d = max(lv.start_date, start_date)
            while d <= min(lv.end_date, end_date):
                leave_map[d] = lv
                d = date.fromordinal(d.toordinal() + 1)

        overtimes = session.query(OvertimeRecord).filter(
            OvertimeRecord.employee_id == employee_id,
            OvertimeRecord.overtime_date >= start_date,
            OvertimeRecord.overtime_date <= end_date,
            OvertimeRecord.is_approved == True
        ).all()
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
                "punch_in": att.punch_in_time.strftime("%H:%M") if att and att.punch_in_time else None,
                "punch_out": att.punch_out_time.strftime("%H:%M") if att and att.punch_out_time else None,
                "status": att.status if att else None,
                "is_late": att.is_late if att else False,
                "late_minutes": att.late_minutes if att else 0,
                "is_early_leave": att.is_early_leave if att else False,
                "leave_type": lv.leave_type if lv else None,
                "leave_type_label": LEAVE_TYPE_LABELS.get(lv.leave_type) if lv else None,
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
            }
        }
    finally:
        session.close()
