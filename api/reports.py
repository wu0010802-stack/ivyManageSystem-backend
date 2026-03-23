"""
Reports router - aggregated statistics for dashboard charts
"""

import logging
from datetime import date
from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy import extract, func, Integer

from models.database import (
    get_session, Attendance, Employee, Classroom, LeaveRecord, SalaryRecord,
)
from services.report_cache_service import report_cache_service
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reports", tags=["reports"])
REPORT_DASHBOARD_CACHE_TTL_SECONDS = 1800


def _query_attendance_monthly(session, start: date, end: date) -> list:
    """查詢並整理年度每月考勤統計（正常/遲到/早退/漏打卡）。"""
    rows = session.query(
        extract("month", Attendance.attendance_date).label("month"),
        func.count(Attendance.id).label("total"),
        func.sum(func.cast(Attendance.is_late, Integer)).label("late"),
        func.sum(func.cast(Attendance.is_early_leave, Integer)).label("early_leave"),
        func.sum(func.cast(Attendance.is_missing_punch_in, Integer) + func.cast(Attendance.is_missing_punch_out, Integer)).label("missing"),
    ).filter(
        Attendance.attendance_date >= start,
        Attendance.attendance_date <= end,
    ).group_by("month").order_by("month").all()

    result = []
    for row in rows:
        total = int(row.total)
        late = int(row.late or 0)
        early = int(row.early_leave or 0)
        missing = int(row.missing or 0)
        anomaly = late + early + missing
        rate = round((total - anomaly) / total * 100, 1) if total > 0 else 0
        result.append({
            "month": int(row.month),
            "total_records": total,
            "normal": total - anomaly,
            "late": late,
            "early_leave": early,
            "missing": missing,
            "rate": rate,
        })
    return result


def _query_attendance_by_classroom(session, start: date, end: date) -> list:
    """查詢並整理各班級年度考勤出勤率。"""
    rows = session.query(
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

    result = []
    for row in rows:
        total = int(row.total)
        late = int(row.late or 0)
        early = int(row.early_leave or 0)
        anomaly = late + early
        rate = round((total - anomaly) / total * 100, 1) if total > 0 else 0
        result.append({
            "classroom": row.classroom,
            "total_records": total,
            "late": late,
            "early_leave": early,
            "rate": rate,
        })
    return result


def _query_leave_monthly(session, start: date, end: date) -> list:
    """查詢並整理年度每月各假別請假統計（12 個月完整列表）。"""
    _EMPTY_MONTH = {
        "personal": 0, "sick": 0, "annual": 0,
        "menstrual": 0, "maternity": 0, "paternity": 0,
        "total_hours": 0,
    }
    rows = session.query(
        extract("month", LeaveRecord.start_date).label("month"),
        LeaveRecord.leave_type,
        func.count(LeaveRecord.id).label("count"),
        func.sum(LeaveRecord.leave_hours).label("total_hours"),
    ).filter(
        LeaveRecord.start_date >= start,
        LeaveRecord.start_date <= end,
    ).group_by("month", LeaveRecord.leave_type).order_by("month").all()

    leave_by_month = defaultdict(lambda: dict(_EMPTY_MONTH))
    for row in rows:
        month = int(row.month)
        lt = row.leave_type
        if lt in leave_by_month[month]:
            leave_by_month[month][lt] = int(row.count)
        leave_by_month[month]["total_hours"] += float(row.total_hours or 0)

    return [{"month": m, **leave_by_month.get(m, dict(_EMPTY_MONTH))} for m in range(1, 13)]


def _query_salary_monthly(session, year: int) -> list:
    """查詢並整理年度每月薪資彙總（總應發、實發、扣款、獎金）。"""
    rows = session.query(
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

    return [
        {
            "month": int(row.month),
            "employee_count": int(row.employee_count),
            "total_gross": round(float(row.total_gross or 0)),
            "total_net": round(float(row.total_net or 0)),
            "total_deductions": round(float(row.total_deductions or 0)),
            "total_bonus": round(float(row.total_bonus or 0)),
            "total_overtime_pay": round(float(row.total_overtime_pay or 0)),
        }
        for row in rows
    ]


def _build_report_dashboard_data(session, year: int) -> dict:
    start = date(year, 1, 1)
    end = date(year, 12, 31)

    return {
        "year": year,
        "attendance_monthly": _query_attendance_monthly(session, start, end),
        "attendance_by_classroom": _query_attendance_by_classroom(session, start, end),
        "leave_monthly": _query_leave_monthly(session, start, end),
        "salary_monthly": _query_salary_monthly(session, year),
    }


@router.get("/dashboard")
def get_report_dashboard(
    year: int = Query(...),
    current_user: dict = Depends(require_permission(Permission.REPORTS)),
):
    """取得年度報表統計資料"""
    session = get_session()
    try:
        return report_cache_service.get_or_build(
            session,
            category="reports_dashboard",
            ttl_seconds=REPORT_DASHBOARD_CACHE_TTL_SECONDS,
            params={"year": year},
            builder=lambda: _build_report_dashboard_data(session, year),
        )
    finally:
        session.close()
