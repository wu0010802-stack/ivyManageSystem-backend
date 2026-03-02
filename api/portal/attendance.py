"""
Portal - attendance sheet endpoint
"""

import calendar as cal_module
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query

from models.database import (
    get_session, Attendance, Classroom, LeaveRecord, OvertimeRecord,
    ShiftAssignment, DailyShift, Holiday,
)
from utils.auth import get_current_user
from ._shared import _get_employee, _get_shift_type_map, WEEKDAY_NAMES, LEAVE_TYPE_LABELS, OVERTIME_TYPE_LABELS

router = APIRouter()


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

        # Determine employee role
        all_classrooms = session.query(Classroom).filter(Classroom.is_active == True).all()
        head_teacher_ids = {c.head_teacher_id for c in all_classrooms if c.head_teacher_id}
        assistant_teacher_ids = set()
        for c in all_classrooms:
            if c.assistant_teacher_id:
                assistant_teacher_ids.add(c.assistant_teacher_id)

        is_head_teacher = emp.id in head_teacher_ids
        is_assistant = emp.id in assistant_teacher_ids
        is_driver = "司機" in emp.title_name
        uses_shift = is_head_teacher or is_assistant

        # Pre-fetch shift assignments for this month's weeks
        shift_schedule_map = {}
        daily_shift_map = {}
        if uses_shift:
            shift_types = _get_shift_type_map(session)

            first_monday = start - timedelta(days=start.weekday())
            last_monday = end - timedelta(days=end.weekday())
            assignments = session.query(ShiftAssignment).filter(
                ShiftAssignment.employee_id == emp.id,
                ShiftAssignment.week_start_date >= first_monday,
                ShiftAssignment.week_start_date <= last_monday,
            ).all()
            for sa in assignments:
                st = shift_types.get(sa.shift_type_id)
                if st:
                    shift_schedule_map[sa.week_start_date] = {
                        "work_start": st.work_start,
                        "work_end": st.work_end,
                        "name": st.name,
                    }

            daily_shifts = session.query(DailyShift).filter(
                DailyShift.employee_id == emp.id,
                DailyShift.date >= start,
                DailyShift.date <= end,
            ).all()
            for ds in daily_shifts:
                st = shift_types.get(ds.shift_type_id)
                if st:
                    daily_shift_map[ds.date] = {
                        "work_start": st.work_start,
                        "work_end": st.work_end,
                        "name": st.name,
                    }

        # Approved leaves for status calculation
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

        # ALL leave requests (any status) for display
        all_leaves = session.query(LeaveRecord).filter(
            LeaveRecord.employee_id == emp.id,
            LeaveRecord.start_date <= end,
            LeaveRecord.end_date >= start,
        ).all()
        leave_request_map = {}
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

        # ALL overtime requests for display
        overtimes = session.query(OvertimeRecord).filter(
            OvertimeRecord.employee_id == emp.id,
            OvertimeRecord.overtime_date >= start,
            OvertimeRecord.overtime_date <= end,
        ).all()
        overtime_map = {}
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

        # Holidays
        holidays_query = session.query(Holiday).filter(
            Holiday.date >= start,
            Holiday.date <= end,
            Holiday.is_active == True
        ).all()
        holiday_map = {h.date: h.name for h in holidays_query}

        grace_minutes = 0
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
                if row["status"] == "no_record":
                     row["status"] = "holiday"

            # Look up shift for this day
            daily_override = daily_shift_map.get(d)
            if daily_override:
                shift_info = daily_override
            else:
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

                # Calculate work hours — Plan B: use shift for missing side
                effective_in = att.punch_in_time
                effective_out = att.punch_out_time

                if not effective_in or not effective_out:
                    # Determine fallback times from shift or defaults
                    if shift_info:
                        fallback_start = datetime.combine(d, datetime.strptime(shift_info["work_start"], "%H:%M").time())
                        fallback_end = datetime.combine(d, datetime.strptime(shift_info["work_end"], "%H:%M").time())
                    else:
                        fb_ws = emp.work_start_time or "08:00"
                        fb_we = emp.work_end_time or "17:00"
                        fallback_start = datetime.combine(d, datetime.strptime(fb_ws, "%H:%M").time())
                        fallback_end = datetime.combine(d, datetime.strptime(fb_we, "%H:%M").time())

                    # 跨夜班修正：排班下班時間落在隔日（如 work_end=02:00 < work_start=18:00）
                    if fallback_end <= fallback_start:
                        fallback_end += timedelta(days=1)

                    if not effective_in:
                        effective_in = fallback_start
                    if not effective_out:
                        effective_out = fallback_end

                # 跨夜班修正：DB 儲存的 punch_out 早於 punch_in（舊資料或異常），補一天
                if effective_in and effective_out and effective_out <= effective_in:
                    effective_out += timedelta(days=1)

                if effective_in and effective_out and effective_out > effective_in:
                    duration_min = (effective_out - effective_in).total_seconds() / 60
                    
                    # 扣除午休時間 (12:00 - 13:00)
                    lunch_start = datetime.combine(d, datetime.strptime("12:00", "%H:%M").time())
                    lunch_end = datetime.combine(d, datetime.strptime("13:00", "%H:%M").time())
                    
                    overlap_start = max(effective_in, lunch_start)
                    overlap_end = min(effective_out, lunch_end)
                    if overlap_end > overlap_start:
                        lunch_overlap_min = (overlap_end - overlap_start).total_seconds() / 60
                        duration_min -= lunch_overlap_min
                        
                    row["work_hours"] = round(duration_min / 60, 1)
                    total_work_hours += row["work_hours"]
                    work_hour_days += 1

                # Recalculate status based on role rules
                if att.punch_in_time and att.punch_out_time:
                    if uses_shift and shift_info:
                        shift_start = datetime.strptime(shift_info["work_start"], "%H:%M").time()
                        shift_end = datetime.strptime(shift_info["work_end"], "%H:%M").time()
                        shift_start_dt = datetime.combine(d, shift_start)
                        shift_end_dt = datetime.combine(d, shift_end)
                        # 跨夜班：排班結束在隔日（如 02:00 < 18:00），補一天
                        if shift_end_dt <= shift_start_dt:
                            shift_end_dt += timedelta(days=1)

                        is_late = att.punch_in_time > shift_start_dt
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
                        required_min = 480 if is_driver else 540
                        duration_min = (att.punch_out_time - att.punch_in_time).total_seconds() / 60

                        if duration_min >= required_min:
                            row["status"] = "normal"
                            row["is_late"] = False
                            row["late_minutes"] = 0
                            row["is_early_leave"] = False
                        else:
                            row["status"] = att.status or "normal"
                            row["is_late"] = att.is_late or False
                            row["late_minutes"] = att.late_minutes or 0
                            row["is_early_leave"] = att.is_early_leave or False
                elif uses_shift and not shift_info:
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
