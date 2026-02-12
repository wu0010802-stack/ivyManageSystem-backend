"""
Reports router - aggregated statistics for dashboard charts
"""

import logging
from datetime import date
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import extract, func, Integer

from models.database import (
    get_session, Attendance, Employee, Classroom, LeaveRecord, SalaryRecord,
)
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("/dashboard")
def get_report_dashboard(
    year: int = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """取得年度報表統計資料"""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="僅限管理員操作")

    session = get_session()
    try:
        start = date(year, 1, 1)
        end = date(year, 12, 31)

        # ---- 1. Monthly Attendance ----
        attendance_records = session.query(
            extract("month", Attendance.attendance_date).label("month"),
            func.count(Attendance.id).label("total"),
            func.sum(func.cast(Attendance.is_late, Integer)).label("late"),
            func.sum(func.cast(Attendance.is_early_leave, Integer)).label("early_leave"),
            func.sum(func.cast(Attendance.is_missing_punch_in, Integer) + func.cast(Attendance.is_missing_punch_out, Integer)).label("missing"),
        ).filter(
            Attendance.attendance_date >= start,
            Attendance.attendance_date <= end,
        ).group_by("month").order_by("month").all()

        attendance_monthly = []
        for row in attendance_records:
            month = int(row.month)
            total = int(row.total)
            late = int(row.late or 0)
            early = int(row.early_leave or 0)
            missing = int(row.missing or 0)
            anomaly = late + early + missing
            rate = round((total - anomaly) / total * 100, 1) if total > 0 else 0
            attendance_monthly.append({
                "month": month,
                "total_records": total,
                "normal": total - anomaly,
                "late": late,
                "early_leave": early,
                "missing": missing,
                "rate": rate,
            })

        # ---- 2. Attendance by Classroom ----
        classroom_rows = session.query(
            Classroom.name.label("classroom"),
            func.count(Attendance.id).label("total"),
            func.sum(func.cast(Attendance.is_late, Integer)).label("late"),
            func.sum(func.cast(Attendance.is_early_leave, Integer)).label("early_leave"),
        ).join(
            Employee, Employee.classroom_id == Classroom.id,
        ).join(
            Attendance, Attendance.employee_id == Employee.id,
        ).filter(
            Attendance.attendance_date >= start,
            Attendance.attendance_date <= end,
            Classroom.is_active == True,
        ).group_by(Classroom.name).order_by(Classroom.name).all()

        attendance_by_classroom = []
        for row in classroom_rows:
            total = int(row.total)
            late = int(row.late or 0)
            early = int(row.early_leave or 0)
            anomaly = late + early
            rate = round((total - anomaly) / total * 100, 1) if total > 0 else 0
            attendance_by_classroom.append({
                "classroom": row.classroom,
                "total_records": total,
                "late": late,
                "early_leave": early,
                "rate": rate,
            })

        # ---- 3. Monthly Leave ----
        leave_records = session.query(
            extract("month", LeaveRecord.start_date).label("month"),
            LeaveRecord.leave_type,
            func.count(LeaveRecord.id).label("count"),
            func.sum(LeaveRecord.leave_hours).label("total_hours"),
        ).filter(
            LeaveRecord.start_date >= start,
            LeaveRecord.start_date <= end,
        ).group_by("month", LeaveRecord.leave_type).order_by("month").all()

        leave_by_month = defaultdict(lambda: {
            "personal": 0, "sick": 0, "annual": 0,
            "menstrual": 0, "maternity": 0, "paternity": 0,
            "total_hours": 0,
        })
        for row in leave_records:
            month = int(row.month)
            lt = row.leave_type
            count = int(row.count)
            hours = float(row.total_hours or 0)
            if lt in leave_by_month[month]:
                leave_by_month[month][lt] = count
            leave_by_month[month]["total_hours"] += hours

        leave_monthly = []
        for m in range(1, 13):
            data = leave_by_month.get(m, {
                "personal": 0, "sick": 0, "annual": 0,
                "menstrual": 0, "maternity": 0, "paternity": 0,
                "total_hours": 0,
            })
            leave_monthly.append({"month": m, **data})

        # ---- 4. Monthly Salary ----
        salary_records = session.query(
            SalaryRecord.salary_month.label("month"),
            func.count(SalaryRecord.id).label("employee_count"),
            func.sum(SalaryRecord.gross_salary).label("total_gross"),
            func.sum(SalaryRecord.net_salary).label("total_net"),
            func.sum(SalaryRecord.total_deduction).label("total_deductions"),
            func.sum(
                func.coalesce(SalaryRecord.festival_bonus, 0) +
                func.coalesce(SalaryRecord.overtime_bonus, 0) +
                func.coalesce(SalaryRecord.performance_bonus, 0) +
                func.coalesce(SalaryRecord.special_bonus, 0)
            ).label("total_bonus"),
            func.sum(func.coalesce(SalaryRecord.overtime_pay, 0)).label("total_overtime_pay"),
        ).filter(
            SalaryRecord.salary_year == year,
        ).group_by(SalaryRecord.salary_month).order_by(SalaryRecord.salary_month).all()

        salary_monthly = []
        for row in salary_records:
            salary_monthly.append({
                "month": int(row.month),
                "employee_count": int(row.employee_count),
                "total_gross": round(float(row.total_gross or 0)),
                "total_net": round(float(row.total_net or 0)),
                "total_deductions": round(float(row.total_deductions or 0)),
                "total_bonus": round(float(row.total_bonus or 0)),
                "total_overtime_pay": round(float(row.total_overtime_pay or 0)),
            })

        return {
            "year": year,
            "attendance_monthly": attendance_monthly,
            "attendance_by_classroom": attendance_by_classroom,
            "leave_monthly": leave_monthly,
            "salary_monthly": salary_monthly,
        }

    finally:
        session.close()
