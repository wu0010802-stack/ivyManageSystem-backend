"""
Portal - salary preview endpoint
"""

import calendar as cal_module
from datetime import date

from fastapi import APIRouter, Depends, Query

from models.database import get_session, Attendance, LeaveRecord, SalaryRecord
from utils.auth import get_current_user
from api.salary_fields import calculate_display_bonus_total, calculate_total_allowances
from ._shared import _get_employee

router = APIRouter()


@router.get("/salary-preview")
def get_salary_preview(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    current_user: dict = Depends(get_current_user),
):
    """取得個人薪資預覽"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        _, last_day = cal_module.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)

        attendances = session.query(Attendance).filter(
            Attendance.employee_id == emp.id,
            Attendance.attendance_date >= start,
            Attendance.attendance_date <= end,
        ).all()

        late_count = sum(1 for a in attendances if a.is_late)
        early_leave_count = sum(1 for a in attendances if a.is_early_leave)
        missing_count = sum(1 for a in attendances if a.is_missing_punch_in or a.is_missing_punch_out)

        leaves = session.query(LeaveRecord).filter(
            LeaveRecord.employee_id == emp.id,
            LeaveRecord.start_date <= end,
            LeaveRecord.end_date >= start,
            LeaveRecord.is_approved == True,
        ).all()
        total_leave_hours = sum(lv.leave_hours for lv in leaves)

        salary = session.query(SalaryRecord).filter(
            SalaryRecord.employee_id == emp.id,
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
        ).first()

        result = {
            "year": year,
            "month": month,
            "attendance_stats": {
                "work_days": len(attendances),
                "late_count": late_count,
                "early_leave_count": early_leave_count,
                "missing_punch_count": missing_count,
                "leave_hours": total_leave_hours,
                "leave_days": round(total_leave_hours / 8, 1),
            },
            "salary": None,
        }

        if salary:
            total_allowances = calculate_total_allowances(salary)
            total_bonus = calculate_display_bonus_total(salary)
            result["salary"] = {
                "base_salary": salary.base_salary,
                "supervisor_allowance": salary.supervisor_allowance or 0,
                "teacher_allowance": salary.teacher_allowance or 0,
                "meal_allowance": salary.meal_allowance or 0,
                "transportation_allowance": salary.transportation_allowance or 0,
                "other_allowance": salary.other_allowance or 0,
                "total_allowances": total_allowances,
                "festival_bonus": salary.festival_bonus or 0,
                "overtime_bonus": salary.overtime_bonus or 0,
                "performance_bonus": salary.performance_bonus or 0,
                "special_bonus": salary.special_bonus or 0,
                "supervisor_dividend": salary.supervisor_dividend or 0,
                "total_bonus": total_bonus,
                "overtime_pay": salary.overtime_pay or 0,
                "meeting_overtime_pay": salary.meeting_overtime_pay or 0,
                "labor_insurance": salary.labor_insurance_employee or 0,
                "health_insurance": salary.health_insurance_employee or 0,
                "late_deduction": salary.late_deduction or 0,
                "early_leave_deduction": salary.early_leave_deduction or 0,
                "attendance_deduction": (salary.late_deduction or 0) + (salary.early_leave_deduction or 0) + (salary.missing_punch_deduction or 0),
                "leave_deduction": salary.leave_deduction or 0,
                "meeting_absence_deduction": salary.meeting_absence_deduction or 0,
                "other_deduction": salary.other_deduction or 0,
                "gross_salary": salary.gross_salary,
                "total_deduction": salary.total_deduction,
                "net_salary": salary.net_salary,
                "is_finalized": salary.is_finalized,
            }

        return result
    finally:
        session.close()
