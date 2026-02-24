"""
Portal - overtime management endpoints
"""

import calendar as cal_module
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import get_session, OvertimeRecord
from utils.auth import get_current_user
from ._shared import _get_employee, OvertimeCreatePortal, OVERTIME_TYPE_LABELS

router = APIRouter()


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


@router.post("/my-overtimes", status_code=201)
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

        from api.overtimes import calculate_overtime_pay, _check_overtime_overlap
        pay = calculate_overtime_pay(emp.base_salary, data.hours, data.overtime_type)

        start_dt = None
        end_dt = None
        if data.start_time:
            h, m = map(int, data.start_time.split(":"))
            start_dt = datetime.combine(data.overtime_date, datetime.min.time().replace(hour=h, minute=m))
        if data.end_time:
            h, m = map(int, data.end_time.split(":"))
            end_dt = datetime.combine(data.overtime_date, datetime.min.time().replace(hour=h, minute=m))

        overlap = _check_overtime_overlap(session, emp.id, data.overtime_date, start_dt, end_dt)
        if overlap:
            st = overlap.start_time.strftime("%H:%M") if overlap.start_time else "未指定"
            et = overlap.end_time.strftime("%H:%M") if overlap.end_time else "未指定"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"您在 {overlap.overtime_date} 已有時間重疊的加班申請"
                    f"（ID: {overlap.id}，{st}～{et}），請勿重複送出"
                ),
            )

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
