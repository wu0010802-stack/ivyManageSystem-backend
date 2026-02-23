"""
Attendance - CRUD endpoints for attendance records
"""

import logging
from calendar import monthrange
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import get_session, Employee, Attendance
from utils.auth import require_permission
from utils.permissions import Permission
from ._shared import AttendanceRecordUpdate

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/records")
async def get_attendance_records(
    year: int = Query(...),
    month: int = Query(...),
    employee_id: Optional[int] = None,
    current_user: dict = Depends(require_permission(Permission.ATTENDANCE)),
):
    """查詢考勤記錄"""
    session = get_session()
    try:
        start_date = date(year, month, 1)
        _, last_day = monthrange(year, month)
        end_date = date(year, month, last_day)

        query = session.query(Attendance, Employee).join(Employee).filter(
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= end_date
        )

        if employee_id:
            query = query.filter(Attendance.employee_id == employee_id)

        query = query.order_by(Employee.name, Attendance.attendance_date)

        records = query.all()

        result = []
        for att, emp in records:
            result.append({
                "id": att.id,
                "employee_id": emp.id,
                "employee_name": emp.name,
                "employee_number": emp.employee_id,
                "date": att.attendance_date.isoformat(),
                "weekday": ["一", "二", "三", "四", "五", "六", "日"][att.attendance_date.weekday()],
                "punch_in": att.punch_in_time.strftime("%H:%M") if att.punch_in_time else None,
                "punch_out": att.punch_out_time.strftime("%H:%M") if att.punch_out_time else None,
                "status": att.status,
                "is_late": att.is_late,
                "is_early_leave": att.is_early_leave,
                "is_missing_punch_in": att.is_missing_punch_in,
                "is_missing_punch_out": att.is_missing_punch_out,
                "late_minutes": att.late_minutes,
                "early_leave_minutes": att.early_leave_minutes,
                "remark": att.remark
            })

        return result
    finally:
        session.close()


@router.post("/record", status_code=201)
async def create_or_update_attendance_record(record: AttendanceRecordUpdate, current_user: dict = Depends(require_permission(Permission.ATTENDANCE))):
    """新增或更新單筆考勤記錄"""
    session = get_session()
    try:
        employee = session.query(Employee).filter(Employee.id == record.employee_id).first()
        if not employee:
            raise HTTPException(status_code=404, detail="找不到員工")

        try:
            attendance_date = datetime.strptime(record.date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="日期格式錯誤，請使用 YYYY-MM-DD")

        punch_in_time = None
        if record.punch_in and record.punch_in.strip():
            try:
                punch_in_time = datetime.combine(
                    attendance_date,
                    datetime.strptime(record.punch_in.strip(), "%H:%M").time()
                )
            except ValueError:
                raise HTTPException(status_code=400, detail="上班時間格式錯誤，請使用 HH:MM")

        punch_out_time = None
        if record.punch_out and record.punch_out.strip():
            try:
                punch_out_time = datetime.combine(
                    attendance_date,
                    datetime.strptime(record.punch_out.strip(), "%H:%M").time()
                )
            except ValueError:
                raise HTTPException(status_code=400, detail="下班時間格式錯誤，請使用 HH:MM")

        work_start = datetime.strptime(employee.work_start_time or "08:00", "%H:%M").time()
        work_end = datetime.strptime(employee.work_end_time or "17:00", "%H:%M").time()
        grace_minutes = 0

        is_late = False
        is_early_leave = False
        is_missing_punch_in = punch_in_time is None
        is_missing_punch_out = punch_out_time is None
        late_minutes = 0
        early_leave_minutes = 0
        status = "normal"

        if punch_in_time:
            work_start_dt = datetime.combine(attendance_date, work_start)
            if punch_in_time > work_start_dt:
                is_late = True
                late_minutes = int((punch_in_time - work_start_dt).total_seconds() / 60)
                status = "late"

        if punch_out_time:
            work_end_dt = datetime.combine(attendance_date, work_end)
            if punch_out_time < work_end_dt:
                is_early_leave = True
                early_leave_minutes = int((work_end_dt - punch_out_time).total_seconds() / 60)
                status = "early_leave" if status == "normal" else status + "+early_leave"

        if is_missing_punch_in:
            status = "missing" if status == "normal" else status + "+missing_in"
        if is_missing_punch_out:
            status = "missing" if status == "normal" else status + "+missing_out"

        existing = session.query(Attendance).filter(
            Attendance.employee_id == employee.id,
            Attendance.attendance_date == attendance_date
        ).first()

        if existing:
            existing.punch_in_time = punch_in_time
            existing.punch_out_time = punch_out_time
            existing.status = status
            existing.is_late = is_late
            existing.is_early_leave = is_early_leave
            existing.is_missing_punch_in = is_missing_punch_in
            existing.is_missing_punch_out = is_missing_punch_out
            existing.late_minutes = late_minutes
            existing.early_leave_minutes = early_leave_minutes
            message = "考勤記錄已更新"
        else:
            attendance = Attendance(
                employee_id=employee.id,
                attendance_date=attendance_date,
                punch_in_time=punch_in_time,
                punch_out_time=punch_out_time,
                status=status,
                is_late=is_late,
                is_early_leave=is_early_leave,
                is_missing_punch_in=is_missing_punch_in,
                is_missing_punch_out=is_missing_punch_out,
                late_minutes=late_minutes,
                early_leave_minutes=early_leave_minutes
            )
            session.add(attendance)
            message = "考勤記錄已新增"

        session.commit()

        return {
            "message": message,
            "status": status,
            "is_late": is_late,
            "late_minutes": late_minutes,
            "is_early_leave": is_early_leave,
            "early_leave_minutes": early_leave_minutes
        }

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/record/{employee_id}/{date}")
async def delete_single_attendance_record(employee_id: int, date: str, current_user: dict = Depends(require_permission(Permission.ATTENDANCE))):
    """刪除單筆考勤記錄"""
    session = get_session()
    try:
        attendance_date = datetime.strptime(date, "%Y-%m-%d").date()

        deleted = session.query(Attendance).filter(
            Attendance.employee_id == employee_id,
            Attendance.attendance_date == attendance_date
        ).delete()

        session.commit()

        if deleted:
            return {"message": "考勤記錄已刪除"}
        else:
            raise HTTPException(status_code=404, detail="找不到該考勤記錄")

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/records/{employee_id}/{date_str}")
def delete_single_attendance(employee_id: int, date_str: str, current_user: dict = Depends(require_permission(Permission.ATTENDANCE))):
    """刪除單筆考勤記錄"""
    session = get_session()
    try:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            target_date = datetime.strptime(date_str, "%Y/%m/%d").date()

        record = session.query(Attendance).filter(
            Attendance.employee_id == employee_id,
            Attendance.attendance_date == target_date
        ).first()

        if not record:
            raise HTTPException(status_code=404, detail="找不到該筆考勤記錄")

        session.delete(record)
        session.commit()
        return {"message": "刪除成功"}

    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式錯誤")
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/records/{year}/{month}")
async def delete_attendance_records(year: int, month: int, current_user: dict = Depends(require_permission(Permission.ATTENDANCE))):
    """刪除指定月份的所有考勤記錄"""
    session = get_session()
    try:
        start_date = date(year, month, 1)
        _, last_day = monthrange(year, month)
        end_date = date(year, month, last_day)

        deleted = session.query(Attendance).filter(
            Attendance.attendance_date >= start_date,
            Attendance.attendance_date <= end_date
        ).delete()

        session.commit()

        return {"message": f"已刪除 {deleted} 筆考勤記錄"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
