"""
Teacher Portal router - personal attendance, leave, overtime, anomalies, salary
"""

import calendar as cal_module
import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from sqlalchemy import or_
from models.database import (
    get_session, Employee, Attendance, LeaveRecord, OvertimeRecord, SalaryRecord,
    Classroom, ShiftAssignment, ShiftType, Student, SchoolEvent,
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
    from datetime import datetime, timedelta

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

        # Determine employee role
        all_classrooms = session.query(Classroom).filter(Classroom.is_active == True).all()
        head_teacher_ids = {c.head_teacher_id for c in all_classrooms if c.head_teacher_id}
        assistant_teacher_ids = set()
        for c in all_classrooms:
            if c.assistant_teacher_id:
                assistant_teacher_ids.add(c.assistant_teacher_id)

        is_head_teacher = emp.id in head_teacher_ids
        is_assistant = emp.id in assistant_teacher_ids
        title_str = (emp.title or "") + (emp.job_title_rel.name if emp.job_title_rel else "")
        is_driver = "司機" in title_str
        uses_shift = is_head_teacher or is_assistant

        # Pre-fetch shift assignments for this month's weeks
        shift_schedule_map = {}  # week_monday -> {work_start, work_end, name}
        if uses_shift:
            # Get all Mondays that cover this month
            first_monday = start - timedelta(days=start.weekday())
            last_monday = end - timedelta(days=end.weekday())
            assignments = session.query(ShiftAssignment).filter(
                ShiftAssignment.employee_id == emp.id,
                ShiftAssignment.week_start_date >= first_monday,
                ShiftAssignment.week_start_date <= last_monday,
            ).all()
            shift_types = {st.id: st for st in session.query(ShiftType).all()}
            for sa in assignments:
                st = shift_types.get(sa.shift_type_id)
                if st:
                    shift_schedule_map[sa.week_start_date] = {
                        "work_start": st.work_start,
                        "work_end": st.work_end,
                        "name": st.name,
                    }

        # Also get leaves for this month (approved only for status calculation)
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

        # Get ALL leave requests (any status) for display
        all_leaves = session.query(LeaveRecord).filter(
            LeaveRecord.employee_id == emp.id,
            LeaveRecord.start_date <= end,
            LeaveRecord.end_date >= start,
        ).all()
        leave_request_map = {}  # date -> list of leave info
        for lv in all_leaves:
            d = max(lv.start_date, start)
            while d <= min(lv.end_date, end):
                if d not in leave_request_map:
                    leave_request_map[d] = []
                leave_request_map[d].append({
                    "leave_type": lv.leave_type,
                    "leave_type_label": LEAVE_TYPE_LABELS.get(lv.leave_type, lv.leave_type),
                    "leave_hours": lv.leave_hours,
                    "is_approved": lv.is_approved,
                    "reason": lv.reason,
                })
                d = date.fromordinal(d.toordinal() + 1)

        # Get ALL overtime requests for display
        overtimes = session.query(OvertimeRecord).filter(
            OvertimeRecord.employee_id == emp.id,
            OvertimeRecord.overtime_date >= start,
            OvertimeRecord.overtime_date <= end,
        ).all()
        overtime_map = {}  # date -> list of overtime info
        for ot in overtimes:
            d = ot.overtime_date
            if d not in overtime_map:
                overtime_map[d] = []
            overtime_map[d].append({
                "overtime_type": ot.overtime_type,
                "overtime_type_label": OVERTIME_TYPE_LABELS.get(ot.overtime_type, ot.overtime_type),
                "hours": ot.hours,
                "is_approved": ot.is_approved,
                "reason": ot.reason,
            })
        
        # Get Holidays
        from models.database import Holiday
        holidays_query = session.query(Holiday).filter(
            Holiday.date >= start,
            Holiday.date <= end,
            Holiday.is_active == True
        ).all()
        holiday_map = {h.date: h.name for h in holidays_query}

        grace_minutes = 5
        days = []
        total_work_hours = 0.0
        work_hour_days = 0

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
                "shift_name": None,
                "scheduled_start": None,
                "scheduled_end": None,
                "work_hours": None,
                "is_holiday": False,
                "holiday_name": None,
                "leave_requests": [],
                "overtime_requests": [],
            }

            # Check if holiday
            if d in holiday_map:
                row["is_holiday"] = True
                row["holiday_name"] = holiday_map[d]
                # Holidays are treated like weekends by default for status
                if row["status"] == "no_record":
                     row["status"] = "holiday"

            # Look up shift for this day
            week_monday = d - timedelta(days=d.weekday())
            shift_info = shift_schedule_map.get(week_monday)
            if shift_info:
                row["shift_name"] = shift_info["name"]
                row["scheduled_start"] = shift_info["work_start"]
                row["scheduled_end"] = shift_info["work_end"]

            att = record_map.get(d)
            if att:
                row["punch_in"] = att.punch_in_time.strftime("%H:%M") if att.punch_in_time else None
                row["punch_out"] = att.punch_out_time.strftime("%H:%M") if att.punch_out_time else None
                row["is_missing_punch_in"] = att.is_missing_punch_in or False
                row["is_missing_punch_out"] = att.is_missing_punch_out or False
                row["remark"] = att.remark

                # Calculate work hours
                if att.punch_in_time and att.punch_out_time:
                    duration_min = (att.punch_out_time - att.punch_in_time).total_seconds() / 60
                    row["work_hours"] = round(duration_min / 60, 1)
                    total_work_hours += row["work_hours"]
                    work_hour_days += 1

                # Recalculate status based on role rules
                if att.punch_in_time and att.punch_out_time:
                    if uses_shift and shift_info:
                        # Head/assistant teacher: compare to shift times
                        shift_start = datetime.strptime(shift_info["work_start"], "%H:%M").time()
                        shift_end = datetime.strptime(shift_info["work_end"], "%H:%M").time()
                        shift_start_dt = datetime.combine(d, shift_start)
                        shift_end_dt = datetime.combine(d, shift_end)
                        grace_dt = shift_start_dt + timedelta(minutes=grace_minutes)

                        is_late = att.punch_in_time > grace_dt
                        is_early_leave = att.punch_out_time < shift_end_dt
                        late_min = max(0, int((att.punch_in_time - shift_start_dt).total_seconds() / 60)) if is_late else 0

                        row["is_late"] = is_late
                        row["late_minutes"] = late_min
                        row["is_early_leave"] = is_early_leave

                        if is_late and is_early_leave:
                            row["status"] = "late+early_leave"
                        elif is_late:
                            row["status"] = "late"
                        elif is_early_leave:
                            row["status"] = "early_leave"
                        else:
                            row["status"] = "normal"
                    else:
                        # Non-teacher: duration-based check
                        required_min = 480 if is_driver else 540
                        duration_min = (att.punch_out_time - att.punch_in_time).total_seconds() / 60

                        if duration_min >= required_min:
                            row["status"] = "normal"
                            row["is_late"] = False
                            row["late_minutes"] = 0
                            row["is_early_leave"] = False
                        else:
                            # Duration insufficient - use DB flags
                            row["status"] = att.status or "normal"
                            row["is_late"] = att.is_late or False
                            row["late_minutes"] = att.late_minutes or 0
                            row["is_early_leave"] = att.is_early_leave or False
                elif uses_shift and not shift_info:
                    # Teacher without shift assignment: fallback to DB flags
                    row["status"] = att.status or "normal"
                    row["is_late"] = att.is_late or False
                    row["late_minutes"] = att.late_minutes or 0
                    row["is_early_leave"] = att.is_early_leave or False
                else:
                    row["status"] = att.status or "normal"
                    row["is_late"] = att.is_late or False
                    row["late_minutes"] = att.late_minutes or 0
                    row["is_early_leave"] = att.is_early_leave or False

                if row["is_missing_punch_in"] or row["is_missing_punch_out"]:
                    if row["status"] == "normal":
                        row["status"] = "missing"
                    elif "missing" not in row["status"]:
                        row["status"] = row["status"] + "+missing"

            if d in leave_dates:
                lt = leave_dates[d]
                row["leave_type"] = lt
                row["leave_type_label"] = LEAVE_TYPE_LABELS.get(lt, lt)
                if not att:
                    row["status"] = "leave"

            # Attach leave and overtime requests for this day
            if d in leave_request_map:
                row["leave_requests"] = leave_request_map[d]
            if d in overtime_map:
                row["overtime_requests"] = overtime_map[d]

            days.append(row)

        # Summary
        total_work = sum(1 for r in days if r["status"] in ("normal", "late") and not r["is_weekend"])
        late_count = sum(1 for r in days if r["is_late"])
        early_leave_count = sum(1 for r in days if r["is_early_leave"])
        missing_punch_count = sum(1 for r in days if r["is_missing_punch_in"] or r["is_missing_punch_out"])
        leave_count = sum(1 for r in days if r["leave_type"] is not None)
        avg_work_hours = round(total_work_hours / work_hour_days, 1) if work_hour_days > 0 else 0

        return {
            "employee_name": emp.name,
            "year": year,
            "month": month,
            "uses_shift": uses_shift,
            "days": days,
            "summary": {
                "total_work_days": total_work,
                "late_count": late_count,
                "early_leave_count": early_leave_count,
                "missing_punch_count": missing_punch_count,
                "leave_count": leave_count,
                "avg_work_hours": avg_work_hours,
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
            is_approved=None,
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


# ============ Leave Stats ============

def _calculate_annual_leave_quota(hire_date: date) -> int:
    """
    根據勞基法計算特休天數 (週年制)
    6個月以上1年未滿者，3日。
    1年以上2年未滿者，7日。
    2年以上3年未滿者，10日。
    3年以上5年未滿者，每年14日。
    5年以上10年未滿者，每年15日。
    10年以上者，每1年加給1日，加至30日為止。
    """
    if not hire_date:
        return 0

    today = date.today()
    
    # Calculate tenure in months and years
    # Simple approximation for months
    months_diff = (today.year - hire_date.year) * 12 + today.month - hire_date.month
    if today.day < hire_date.day:
        months_diff -= 1
        
    years = months_diff // 12
    
    if months_diff < 6:
        return 0
    elif 6 <= months_diff < 12:
        return 3
    elif 1 <= years < 2:
        return 7
    elif 2 <= years < 3:
        return 10
    elif 3 <= years < 5:
        return 14
    elif 5 <= years < 10:
        return 15
    else:
        # 10 years or more
        extra_days = years - 10
        total = 15 + extra_days
        return min(total, 30)

@router.get("/my-leave-stats")
def get_my_leave_stats(
    current_user: dict = Depends(get_current_user),
):
    """取得個人特休統計 (年資、特休天數、已休天數)"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        
        # Calculate Seniority
        hire_date = emp.hire_date
        seniority_years = 0
        seniority_months = 0
        annual_leave_quota = 0
        
        if hire_date:
            today = date.today()
            months_diff = (today.year - hire_date.year) * 12 + today.month - hire_date.month
            if today.day < hire_date.day:
                months_diff -= 1
            
            seniority_years = months_diff // 12
            seniority_months = months_diff % 12
            annual_leave_quota = _calculate_annual_leave_quota(hire_date)

        # Calculate used annual leave in current calendar year
        # Note: This is a simplification. Ideally should track by "leave year" cycle.
        # But for display purposes, we often show "This Year's Usage".
        current_year = date.today().year
        start_of_year = date(current_year, 1, 1)
        end_of_year = date(current_year, 12, 31)
        
        used_leaves = session.query(LeaveRecord).filter(
            LeaveRecord.employee_id == emp.id,
            LeaveRecord.leave_type == "annual",
            LeaveRecord.start_date >= start_of_year,
            LeaveRecord.start_date <= end_of_year,
            LeaveRecord.is_approved == True,  # Only count approved ones? Or all? Usually approved.
        ).all()
        
        used_days = sum(lv.leave_hours for lv in used_leaves) / 8.0  # Assuming 8hr days
        
        return {
            "hire_date": hire_date.isoformat() if hire_date else None,
            "seniority_years": seniority_years,
            "seniority_months": seniority_months,
            "annual_leave_quota": annual_leave_quota,
            "annual_leave_used_days": round(used_days, 1),
            "start_of_calculation": start_of_year.isoformat(), 
            "end_of_calculation": end_of_year.isoformat()
        }
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
            is_approved=None,
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


# ============ My Students ============

@router.get("/my-students")
def get_my_students(
    classroom_id: Optional[int] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """取得教師所屬班級的學生資料"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        # Find classrooms where this teacher is assigned
        query = session.query(Classroom).filter(
            Classroom.is_active == True,
            or_(
                Classroom.head_teacher_id == emp.id,
                Classroom.assistant_teacher_id == emp.id,
                Classroom.art_teacher_id == emp.id,
            ),
        )
        if classroom_id:
            query = query.filter(Classroom.id == classroom_id)

        classrooms = query.all()

        result = []
        for cr in classrooms:
            # Determine teacher's role in this classroom
            role = "教師"
            if cr.head_teacher_id == emp.id:
                role = "主教老師"
            elif cr.assistant_teacher_id == emp.id:
                role = "助教老師"
            elif cr.art_teacher_id == emp.id:
                role = "美術老師"

            students = session.query(Student).filter(
                Student.classroom_id == cr.id,
                Student.is_active == True,
            ).order_by(Student.name).all()

            result.append({
                "classroom_id": cr.id,
                "classroom_name": cr.name,
                "role": role,
                "student_count": len(students),
                "students": [{
                    "id": s.id,
                    "student_id": s.student_id,
                    "name": s.name,
                    "gender": s.gender,
                    "birthday": s.birthday.isoformat() if s.birthday else None,
                    "enrollment_date": s.enrollment_date.isoformat() if s.enrollment_date else None,
                    "parent_name": s.parent_name,
                    "parent_phone": s.parent_phone,
                    "address": s.address,
                    "status_tag": s.status_tag,
                    "notes": s.notes,
                } for s in students],
            })

        return {
            "employee_name": emp.name,
            "classrooms": result,
            "total_students": sum(c["student_count"] for c in result),
        }
    finally:
        session.close()


# ============ School Calendar ============

@router.get("/calendar")
def get_portal_calendar(
    year: int = Query(...),
    month: int = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """取得學校行事曆（教師檢視）"""
    session = get_session()
    try:
        _, last_day = cal_module.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)

        events = session.query(SchoolEvent).filter(
            SchoolEvent.is_active == True,
            SchoolEvent.event_date <= end,
            or_(
                SchoolEvent.end_date >= start,
                (SchoolEvent.end_date.is_(None)) & (SchoolEvent.event_date >= start),
            ),
        ).order_by(SchoolEvent.event_date).all()

        EVENT_TYPE_LABELS_LOCAL = {
            "meeting": "會議",
            "activity": "活動",
            "holiday": "假日",
            "general": "一般",
        }

        return [{
            "id": ev.id,
            "title": ev.title,
            "description": ev.description,
            "event_date": ev.event_date.isoformat(),
            "end_date": ev.end_date.isoformat() if ev.end_date else None,
            "event_type": ev.event_type,
            "event_type_label": EVENT_TYPE_LABELS_LOCAL.get(ev.event_type, ev.event_type),
            "is_all_day": ev.is_all_day,
            "start_time": ev.start_time,
            "end_time": ev.end_time,
            "location": ev.location,
        } for ev in events]
    finally:
        session.close()
