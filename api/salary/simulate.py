"""
api/salary/simulate.py — 薪資試算（沙盒模式，不寫入 DB）

含 1 個 endpoint + 2 個 schema + 4 個 helper：
- POST /salaries/simulate

所有 symbol 僅在本模組內使用（HTTP 測試走 TestClient，無外部直接 import），
不需在 api.salary.__init__ re-export。

模組級服務注入狀態 `_salary_engine` 由 api.salary.__init__ 維護；本檔在
endpoint 內以 lazy import 取最新值，避免拿到 None reference。
"""

import calendar as _calendar
import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import joinedload

from models.base import session_scope
from models.database import Attendance, Employee, SalaryRecord
from services.salary.engine import SalaryEngine as RuntimeSalaryEngine
from services.salary.utils import calc_daily_salary
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.salary_access import (
    enforce_self_or_full_salary as _enforce_self_or_full_salary,
)
from utils.rounding import round_half_up

logger = logging.getLogger(__name__)
router = APIRouter()


# ============ Pydantic Models ============


class SalarySimulateOverride(BaseModel):
    """薪資試算覆蓋參數（None = 使用 DB 實際資料）"""

    late_count: Optional[int] = Field(None, ge=0)
    early_leave_count: Optional[int] = Field(None, ge=0)
    missing_punch_count: Optional[int] = Field(None, ge=0)
    total_late_minutes: Optional[float] = Field(None, ge=0)
    total_early_minutes: Optional[float] = Field(None, ge=0)
    work_days: Optional[int] = Field(None, ge=0, le=31)
    extra_personal_leave_hours: float = Field(0, ge=0)
    extra_sick_leave_hours: float = Field(0, ge=0)
    enrollment_override: Optional[int] = Field(None, ge=0)
    extra_overtime_pay: float = Field(0, ge=0)


class SalarySimulateRequest(BaseModel):
    employee_id: int = Field(..., ge=1)
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    overrides: SalarySimulateOverride = SalarySimulateOverride()


# ============ Display dict helpers ============


_SIMULATE_COMPARE_KEYS = [
    "base_salary",
    "festival_bonus",
    "overtime_bonus",
    "overtime_pay",
    "late_deduction",
    "early_leave_deduction",
    "leave_deduction",
    "absence_deduction",
    "gross_salary",
    "total_deductions",
    "net_pay",
]


def _build_salary_display_dict(**kwargs) -> dict:
    """共用薪資欄位 dict 建構器，確保兩個轉換函式的欄位名稱與順序一致。"""
    keys = [
        "base_salary",
        "festival_bonus",
        "overtime_bonus",
        "overtime_pay",
        "meeting_overtime_pay",
        "birthday_bonus",
        "supervisor_dividend",
        "labor_insurance",
        "health_insurance",
        "pension_self",
        "late_deduction",
        "early_leave_deduction",
        "missing_punch_deduction",
        "leave_deduction",
        "absence_deduction",
        "meeting_absence_deduction",
        "gross_salary",
        "total_deductions",
        "net_pay",
        "late_count",
        "early_leave_count",
        "missing_punch_count",
    ]
    result = {k: kwargs.get(k, 0) for k in keys}
    # canonical 別名，確保所有端點欄位一致
    result["net_salary"] = result["net_pay"]
    result["total_deduction"] = result["total_deductions"]
    return result


def _breakdown_to_simulate_dict(
    breakdown, late_count: int, early_count: int, missing_count: int
) -> dict:
    return _build_salary_display_dict(
        base_salary=breakdown.base_salary,
        festival_bonus=breakdown.festival_bonus,
        overtime_bonus=breakdown.overtime_bonus,
        overtime_pay=breakdown.overtime_work_pay,
        meeting_overtime_pay=breakdown.meeting_overtime_pay or 0,
        birthday_bonus=breakdown.birthday_bonus or 0,
        supervisor_dividend=breakdown.supervisor_dividend,
        labor_insurance=breakdown.labor_insurance,
        health_insurance=breakdown.health_insurance,
        pension_self=breakdown.pension_self or 0,
        late_deduction=breakdown.late_deduction,
        early_leave_deduction=breakdown.early_leave_deduction,
        missing_punch_deduction=breakdown.missing_punch_deduction,
        leave_deduction=breakdown.leave_deduction,
        absence_deduction=breakdown.absence_deduction or 0,
        meeting_absence_deduction=breakdown.meeting_absence_deduction or 0,
        gross_salary=breakdown.gross_salary,
        total_deductions=breakdown.total_deduction,
        net_pay=breakdown.net_salary,
        late_count=late_count,
        early_leave_count=early_count,
        missing_punch_count=missing_count,
    )


def _record_to_actual_dict(record) -> dict:
    return _build_salary_display_dict(
        base_salary=record.base_salary or 0,
        festival_bonus=record.festival_bonus or 0,
        overtime_bonus=record.overtime_bonus or 0,
        overtime_pay=record.overtime_pay or 0,
        meeting_overtime_pay=record.meeting_overtime_pay or 0,
        birthday_bonus=record.birthday_bonus or 0,
        supervisor_dividend=record.supervisor_dividend or 0,
        labor_insurance=record.labor_insurance_employee or 0,
        health_insurance=record.health_insurance_employee or 0,
        pension_self=record.pension_employee or 0,
        late_deduction=record.late_deduction or 0,
        early_leave_deduction=record.early_leave_deduction or 0,
        missing_punch_deduction=record.missing_punch_deduction or 0,
        leave_deduction=record.leave_deduction or 0,
        absence_deduction=record.absence_deduction or 0,
        meeting_absence_deduction=record.meeting_absence_deduction or 0,
        gross_salary=record.gross_salary or 0,
        total_deductions=record.total_deduction or 0,
        net_pay=record.net_salary or 0,
        late_count=record.late_count or 0,
        early_leave_count=record.early_leave_count or 0,
        missing_punch_count=record.missing_punch_count or 0,
    )


# ============ Routes ============


@router.post("/salaries/simulate")
def simulate_salary(
    req: SalarySimulateRequest,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
):
    """薪資試算（沙盒模式，不寫入 DB）——套用覆蓋參數後回傳計算結果與實際紀錄對比。"""
    # Lazy import：_salary_engine 是 init_salary_services 注入的 module-level state，
    # 必須在 request 處理時才取最新值（import-time 抓會拿到 None）。
    from . import _salary_engine

    # 權限檢查置於最前:即使薪資引擎未初始化,也不應對 non-admin/hr 洩漏「他人 employee_id 是否存在」之類的訊號
    _enforce_self_or_full_salary(current_user, req.employee_id)
    if not _salary_engine:
        raise HTTPException(status_code=503, detail="薪資引擎尚未初始化")

    engine: RuntimeSalaryEngine = _salary_engine
    ov = req.overrides
    year, month = req.year, req.month

    _, last_day = _calendar.monthrange(year, month)
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)

    with session_scope() as session:
        emp = (
            session.query(Employee)
            .options(joinedload(Employee.job_title_rel))
            .filter(Employee.id == req.employee_id)
            .first()
        )
        if not emp:
            raise HTTPException(
                status_code=404, detail=f"員工 id={req.employee_id} 不存在"
            )
        if emp.employee_type == "hourly":
            raise HTTPException(status_code=422, detail="時薪制員工不支援試算功能")

        emp_dict = engine._load_emp_dict(emp)

        # 查詢實際考勤
        attendances = (
            session.query(Attendance)
            .filter(
                Attendance.employee_id == emp.id,
                Attendance.attendance_date >= start_date,
                Attendance.attendance_date <= end_date,
            )
            .all()
        )

        # 實際考勤統計
        actual_late_count = sum(1 for a in attendances if a.is_late)
        actual_early_count = sum(1 for a in attendances if a.is_early_leave)
        actual_missing = sum(
            1 for a in attendances if a.is_missing_punch_in or a.is_missing_punch_out
        )
        actual_late_min = sum(a.late_minutes or 0 for a in attendances if a.is_late)
        actual_early_min = sum(
            a.early_leave_minutes or 0 for a in attendances if a.is_early_leave
        )

        # 套用覆蓋
        sim_late_count = (
            ov.late_count if ov.late_count is not None else actual_late_count
        )
        sim_early_count = (
            ov.early_leave_count
            if ov.early_leave_count is not None
            else actual_early_count
        )
        sim_missing = (
            ov.missing_punch_count
            if ov.missing_punch_count is not None
            else actual_missing
        )
        sim_late_min = (
            ov.total_late_minutes
            if ov.total_late_minutes is not None
            else actual_late_min
        )
        sim_early_min = (
            ov.total_early_minutes
            if ov.total_early_minutes is not None
            else actual_early_min
        )
        sim_work_days = ov.work_days if ov.work_days is not None else len(attendances)

        # 將分鐘數均分給每次遲到（給 _calculate_deductions 用）
        emp_dict["_late_details"] = (
            [sim_late_min / sim_late_count] * sim_late_count
            if sim_late_count > 0
            else []
        )

        from services.attendance_parser import AttendanceResult

        attendance_result = AttendanceResult(
            employee_name=emp.name,
            total_days=sim_work_days,
            normal_days=max(0, sim_work_days - sim_late_count - sim_early_count),
            late_count=sim_late_count,
            early_leave_count=sim_early_count,
            missing_punch_in_count=sim_missing,
            missing_punch_out_count=0,
            total_late_minutes=int(sim_late_min),
            total_early_minutes=int(sim_early_min),
            details=[],
        )

        classroom_context, office_staff_context = engine._build_contexts(
            session, emp, end_date
        )
        if ov.enrollment_override is not None:
            if classroom_context:
                classroom_context["current_enrollment"] = ov.enrollment_override
            if office_staff_context:
                office_staff_context["school_enrollment"] = ov.enrollment_override

        daily_salary = calc_daily_salary(emp_dict["base_salary"])
        period_records = engine._load_period_records(
            session, emp, start_date, end_date, year, month, daily_salary
        )

        extra_leave_hours = ov.extra_personal_leave_hours + ov.extra_sick_leave_hours
        hourly_rate = calc_daily_salary(emp_dict["base_salary"]) / 8
        leave_deduction = (
            period_records["leave_deduction"] + extra_leave_hours * hourly_rate
        )
        personal_sick_hours = (
            period_records["personal_sick_leave_hours"] + extra_leave_hours
        )
        overtime_pay = period_records["overtime_work_pay"] + ov.extra_overtime_pay

        absent_count, absence_amount = engine._detect_absences(
            session,
            emp,
            attendances,
            period_records["approved_leaves"],
            start_date,
            end_date,
            year,
            month,
        )

        # 發放月（2/6/9/12）試算須與正式落帳口徑一致：
        # 用期間累積的 festival/overtime 覆蓋單月值，否則 simulate vs actual 的 diff
        # 在發放月會被「單月 vs 期間累積」的口徑差異污染。
        period_festival_total, period_overtime_total = (
            engine._compute_period_accrual_totals(session, emp, year, month)
        )

        breakdown = engine.calculate_salary(
            employee=emp_dict,
            year=year,
            month=month,
            attendance=attendance_result,
            leave_deduction=leave_deduction,
            classroom_context=classroom_context,
            office_staff_context=office_staff_context,
            meeting_context=period_records["meeting_context"],
            overtime_work_pay=overtime_pay,
            personal_sick_leave_hours=personal_sick_hours,
            period_festival_override=period_festival_total,
            period_overtime_override=period_overtime_total,
        )
        breakdown.absent_count = absent_count
        breakdown.absence_deduction = round_half_up(absence_amount)
        breakdown.total_deduction = round_half_up(breakdown.total_deduction + absence_amount)
        breakdown.net_salary = breakdown.gross_salary - breakdown.total_deduction

        actual_record = (
            session.query(SalaryRecord)
            .filter(
                SalaryRecord.employee_id == emp.id,
                SalaryRecord.salary_year == year,
                SalaryRecord.salary_month == month,
            )
            .first()
        )

        simulated = _breakdown_to_simulate_dict(
            breakdown, sim_late_count, sim_early_count, sim_missing
        )
        actual = _record_to_actual_dict(actual_record) if actual_record else None
        diff = (
            {
                k: round_half_up(simulated.get(k, 0) - actual.get(k, 0))
                for k in _SIMULATE_COMPARE_KEYS
            }
            if actual
            else None
        )

        overrides_active = [
            k for k, v in ov.model_dump().items() if v is not None and v != 0
        ]

        return {
            "employee": {
                "id": emp.id,
                "name": emp.name,
                "employee_id": emp.employee_id,
                "job_title": emp.title_name,
            },
            "period": {"year": year, "month": month},
            "overrides_active": overrides_active,
            "simulated": simulated,
            "actual": actual,
            "diff": diff,
        }
