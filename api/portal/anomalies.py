"""
Portal - anomaly endpoints
"""

import calendar as cal_module
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500

from models.database import get_session, Attendance, LeaveRecord
from utils.auth import get_current_user
from ._shared import _get_employee, AnomalyConfirm, WEEKDAY_NAMES

router = APIRouter()


@router.get("/anomalies")
def get_anomalies(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
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

        from services.salary_engine import get_working_days
        wd = get_working_days(year, month, session)
        daily_salary = emp.base_salary / wd if emp.base_salary else 0

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
                    "detail": "上班未打卡（不扣款，僅記錄）",
                    "estimated_deduction": 0,
                })
            if att.is_missing_punch_out:
                items.append({
                    "type": "missing_punch",
                    "type_label": "未打卡(下班)",
                    "detail": "下班未打卡（不扣款，僅記錄）",
                    "estimated_deduction": 0,
                })

            for item in items:
                anomalies.append({
                    "id": att.id,
                    "date": att.attendance_date.isoformat(),
                    "weekday": WEEKDAY_NAMES[att.attendance_date.weekday()],
                    "confirmed": att.confirmed_action is not None,
                    "confirmed_action": att.confirmed_action,
                    "confirmed_at": att.confirmed_at.isoformat() if att.confirmed_at else None,
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

        att.confirmed_action = data.action
        att.confirmed_by = emp.name
        att.confirmed_at = datetime.now()

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
        raise_safe_500(e)
    finally:
        session.close()
