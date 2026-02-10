"""
Teacher Portal router - personal attendance, leave, overtime, anomalies, salary
"""

import calendar as cal_module
import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from models.database import (
    get_session, Employee, Attendance, LeaveRecord, OvertimeRecord, SalaryRecord,
)
from utils.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/portal", tags=["portal"])

WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]

LEAVE_TYPE_LABELS = {
    "personal": "事假",
    "sick": "病假",
    "menstrual": "生理假",
    "annual": "特休",
    "maternity": "產假",
    "paternity": "陪產假",
}

OVERTIME_TYPE_LABELS = {
    "weekday": "平日",
    "weekend": "假日",
    "holiday": "國定假日",
}


# ============ Pydantic Models ============

class LeaveCreatePortal(BaseModel):
    leave_type: str
    start_date: date
    end_date: date
    leave_hours: float = 8
    reason: Optional[str] = None


class OvertimeCreatePortal(BaseModel):
    overtime_date: date
    overtime_type: str
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    hours: float
    reason: Optional[str] = None


class AnomalyConfirm(BaseModel):
    action: str  # "use_pto" | "accept" | "dispute"
    remark: Optional[str] = None


# ============ Helper ============

def _get_employee(session, current_user: dict) -> Employee:
    emp = session.query(Employee).filter(Employee.id == current_user["employee_id"]).first()
    if not emp:
        raise HTTPException(status_code=404, detail="找不到對應的員工資料")
    return emp


# ============ Attendance Sheet ============

@router.get("/attendance-sheet")
def get_attendance_sheet(
    year: int = Query(...),
    month: int = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """取得個人月考勤表"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        _, last_day = cal_module.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)

        records = session.query(Attendance).filter(
            Attendance.employee_id == emp.id,
            Attendance.attendance_date >= start,
            Attendance.attendance_date <= end,
        ).order_by(Attendance.attendance_date).all()

        # Build lookup
        record_map = {r.attendance_date: r for r in records}

        # Also get leaves for this month
        leaves = session.query(LeaveRecord).filter(
            LeaveRecord.employee_id == emp.id,
            LeaveRecord.start_date <= end,
            LeaveRecord.end_date >= start,
            LeaveRecord.is_approved == True,
        ).all()
        leave_dates = {}
        for lv in leaves:
            d = max(lv.start_date, start)
            while d <= min(lv.end_date, end):
                leave_dates[d] = lv.leave_type
                d = date.fromordinal(d.toordinal() + 1)

        days = []
        for day_num in range(1, last_day + 1):
            d = date(year, month, day_num)
            weekday = d.weekday()
            weekday_name = WEEKDAY_NAMES[weekday]
            is_weekend = weekday >= 5

            row = {
                "date": d.isoformat(),
                "day": day_num,
                "weekday": weekday_name,
                "is_weekend": is_weekend,
                "punch_in": None,
                "punch_out": None,
                "status": "weekend" if is_weekend else "no_record",
                "is_late": False,
                "late_minutes": 0,
                "is_early_leave": False,
                "is_missing_punch_in": False,
                "is_missing_punch_out": False,
                "leave_type": None,
                "leave_type_label": None,
                "remark": None,
            }

            att = record_map.get(d)
            if att:
                row["punch_in"] = att.punch_in_time.strftime("%H:%M") if att.punch_in_time else None
                row["punch_out"] = att.punch_out_time.strftime("%H:%M") if att.punch_out_time else None
                row["status"] = att.status or "normal"
                row["is_late"] = att.is_late or False
                row["late_minutes"] = att.late_minutes or 0
                row["is_early_leave"] = att.is_early_leave or False
                row["is_missing_punch_in"] = att.is_missing_punch_in or False
                row["is_missing_punch_out"] = att.is_missing_punch_out or False
                row["remark"] = att.remark

            if d in leave_dates:
                lt = leave_dates[d]
                row["leave_type"] = lt
                row["leave_type_label"] = LEAVE_TYPE_LABELS.get(lt, lt)
                if not att:
                    row["status"] = "leave"

            days.append(row)

        # Summary
        total_work = sum(1 for r in days if r["status"] in ("normal", "late") and not r["is_weekend"])
        late_count = sum(1 for r in days if r["is_late"])
        early_leave_count = sum(1 for r in days if r["is_early_leave"])
        missing_punch_count = sum(1 for r in days if r["is_missing_punch_in"] or r["is_missing_punch_out"])
        leave_count = sum(1 for r in days if r["leave_type"] is not None)

        return {
            "employee_name": emp.name,
            "year": year,
            "month": month,
            "days": days,
            "summary": {
                "total_work_days": total_work,
                "late_count": late_count,
                "early_leave_count": early_leave_count,
                "missing_punch_count": missing_punch_count,
                "leave_count": leave_count,
            },
        }
    finally:
        session.close()


# ============ Anomalies ============

@router.get("/anomalies")
def get_anomalies(
    year: int = Query(...),
    month: int = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """取得出勤異常列表"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        _, last_day = cal_module.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)

        records = session.query(Attendance).filter(
            Attendance.employee_id == emp.id,
            Attendance.attendance_date >= start,
            Attendance.attendance_date <= end,
        ).all()

        daily_salary = emp.base_salary / 30 if emp.base_salary else 0

        anomalies = []
        for att in records:
            items = []
            if att.is_late and att.late_minutes and att.late_minutes > 0:
                deduction = round(daily_salary / 8 / 60 * att.late_minutes)
                items.append({
                    "type": "late",
                    "type_label": "遲到",
                    "detail": f"遲到 {att.late_minutes} 分鐘",
                    "estimated_deduction": deduction,
                })
            if att.is_early_leave:
                items.append({
                    "type": "early_leave",
                    "type_label": "早退",
                    "detail": "早退",
                    "estimated_deduction": 50,
                })
            if att.is_missing_punch_in:
                items.append({
                    "type": "missing_punch",
                    "type_label": "未打卡(上班)",
                    "detail": "上班未打卡",
                    "estimated_deduction": 50,
                })
            if att.is_missing_punch_out:
                items.append({
                    "type": "missing_punch",
                    "type_label": "未打卡(下班)",
                    "detail": "下班未打卡",
                    "estimated_deduction": 50,
                })

            for item in items:
                anomalies.append({
                    "id": att.id,
                    "date": att.attendance_date.isoformat(),
                    "weekday": WEEKDAY_NAMES[att.attendance_date.weekday()],
                    "confirmed": False,
                    **item,
                })

        return anomalies
    finally:
        session.close()


@router.post("/anomalies/{attendance_id}/confirm")
def confirm_anomaly(
    attendance_id: int,
    data: AnomalyConfirm,
    current_user: dict = Depends(get_current_user),
):
    """確認出勤異常處理方式"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        att = session.query(Attendance).filter(
            Attendance.id == attendance_id,
            Attendance.employee_id == emp.id,
        ).first()
        if not att:
            raise HTTPException(status_code=404, detail="找不到該考勤記錄")

        if data.action == "use_pto":
            # Create annual leave for this day
            leave = LeaveRecord(
                employee_id=emp.id,
                leave_type="annual",
                start_date=att.attendance_date,
                end_date=att.attendance_date,
                leave_hours=8,
                reason=f"以特休抵銷異常 ({data.remark or ''})",
                is_approved=False,
            )
            session.add(leave)
            att.remark = (att.remark or "") + " [已申請特休抵銷]"
        elif data.action == "accept":
            att.remark = (att.remark or "") + " [已確認接受扣款]"
        elif data.action == "dispute":
            att.remark = (att.remark or "") + f" [申訴: {data.remark or ''}]"
        else:
            raise HTTPException(status_code=400, detail="無效的處理方式")

        session.commit()

        msg = {
            "use_pto": "已送出特休申請，待主管核准",
            "accept": "已確認接受扣款",
            "dispute": "申訴已提交，待管理員處理",
        }
        return {"message": msg.get(data.action, "處理完成")}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ============ My Leaves ============

@router.get("/my-leaves")
def get_my_leaves(
    year: int = Query(...),
    month: int = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """取得個人請假記錄"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        _, last_day = cal_module.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)

        leaves = session.query(LeaveRecord).filter(
            LeaveRecord.employee_id == emp.id,
            LeaveRecord.start_date <= end,
            LeaveRecord.end_date >= start,
        ).order_by(LeaveRecord.start_date.desc()).all()

        return [{
            "id": lv.id,
            "leave_type": lv.leave_type,
            "leave_type_label": LEAVE_TYPE_LABELS.get(lv.leave_type, lv.leave_type),
            "start_date": lv.start_date.isoformat(),
            "end_date": lv.end_date.isoformat(),
            "leave_hours": lv.leave_hours,
            "reason": lv.reason,
            "is_approved": lv.is_approved,
            "approved_by": lv.approved_by,
            "created_at": lv.created_at.isoformat() if lv.created_at else None,
        } for lv in leaves]
    finally:
        session.close()


@router.post("/my-leaves")
def create_my_leave(
    data: LeaveCreatePortal,
    current_user: dict = Depends(get_current_user),
):
    """提交請假申請"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        if data.leave_type not in LEAVE_TYPE_LABELS:
            raise HTTPException(status_code=400, detail=f"無效的假別: {data.leave_type}")
        if data.end_date < data.start_date:
            raise HTTPException(status_code=400, detail="結束日期不可早於開始日期")

        leave = LeaveRecord(
            employee_id=emp.id,
            leave_type=data.leave_type,
            start_date=data.start_date,
            end_date=data.end_date,
            leave_hours=data.leave_hours,
            reason=data.reason,
            is_approved=False,
        )
        session.add(leave)
        session.commit()
        return {"message": "請假申請已送出，待主管核准", "id": leave.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ============ My Overtimes ============

@router.get("/my-overtimes")
def get_my_overtimes(
    year: int = Query(...),
    month: int = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """取得個人加班記錄"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        _, last_day = cal_module.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)

        records = session.query(OvertimeRecord).filter(
            OvertimeRecord.employee_id == emp.id,
            OvertimeRecord.overtime_date >= start,
            OvertimeRecord.overtime_date <= end,
        ).order_by(OvertimeRecord.overtime_date.desc()).all()

        return [{
            "id": ot.id,
            "overtime_date": ot.overtime_date.isoformat(),
            "overtime_type": ot.overtime_type,
            "overtime_type_label": OVERTIME_TYPE_LABELS.get(ot.overtime_type, ot.overtime_type),
            "start_time": ot.start_time.strftime("%H:%M") if ot.start_time else None,
            "end_time": ot.end_time.strftime("%H:%M") if ot.end_time else None,
            "hours": ot.hours,
            "overtime_pay": ot.overtime_pay,
            "reason": ot.reason,
            "is_approved": ot.is_approved,
            "approved_by": ot.approved_by,
            "created_at": ot.created_at.isoformat() if ot.created_at else None,
        } for ot in records]
    finally:
        session.close()


@router.post("/my-overtimes")
def create_my_overtime(
    data: OvertimeCreatePortal,
    current_user: dict = Depends(get_current_user),
):
    """提交加班申請"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        if data.overtime_type not in OVERTIME_TYPE_LABELS:
            raise HTTPException(status_code=400, detail=f"無效的加班類型: {data.overtime_type}")

        from api.overtimes import calculate_overtime_pay
        from datetime import datetime
        pay = calculate_overtime_pay(emp.base_salary, data.hours, data.overtime_type)

        start_dt = None
        end_dt = None
        if data.start_time:
            h, m = map(int, data.start_time.split(":"))
            start_dt = datetime.combine(data.overtime_date, datetime.min.time().replace(hour=h, minute=m))
        if data.end_time:
            h, m = map(int, data.end_time.split(":"))
            end_dt = datetime.combine(data.overtime_date, datetime.min.time().replace(hour=h, minute=m))

        ot = OvertimeRecord(
            employee_id=emp.id,
            overtime_date=data.overtime_date,
            overtime_type=data.overtime_type,
            start_time=start_dt,
            end_time=end_dt,
            hours=data.hours,
            overtime_pay=pay,
            reason=data.reason,
            is_approved=False,
        )
        session.add(ot)
        session.commit()
        return {"message": "加班申請已送出，待主管核准", "id": ot.id, "overtime_pay": pay}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ============ Salary Preview ============

@router.get("/salary-preview")
def get_salary_preview(
    year: int = Query(...),
    month: int = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """取得個人薪資預覽"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        # Attendance stats
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

        # Salary record
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
            total_allowances = (
                (salary.supervisor_allowance or 0) +
                (salary.teacher_allowance or 0) +
                (salary.meal_allowance or 0) +
                (salary.transportation_allowance or 0) +
                (salary.other_allowance or 0)
            )
            total_bonus = (
                (salary.festival_bonus or 0) +
                (salary.overtime_bonus or 0) +
                (salary.performance_bonus or 0) +
                (salary.special_bonus or 0)
            )
            result["salary"] = {
                "base_salary": salary.base_salary,
                "total_allowances": total_allowances,
                "total_bonus": total_bonus,
                "overtime_pay": salary.overtime_pay or 0,
                "labor_insurance": salary.labor_insurance_employee or 0,
                "health_insurance": salary.health_insurance_employee or 0,
                "late_deduction": salary.late_deduction or 0,
                "leave_deduction": salary.leave_deduction or 0,
                "other_deduction": salary.other_deduction or 0,
                "gross_salary": salary.gross_salary,
                "total_deduction": salary.total_deduction,
                "net_salary": salary.net_salary,
                "is_finalized": salary.is_finalized,
            }

        return result
    finally:
        session.close()
