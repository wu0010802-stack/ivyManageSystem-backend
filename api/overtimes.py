"""
Overtime management router
"""

import logging
import calendar as cal_module
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from models.database import get_session, Employee, OvertimeRecord

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["overtimes"])


# ============ Constants ============

OVERTIME_TYPE_LABELS = {
    "weekday": "平日",
    "weekend": "假日",
    "holiday": "國定假日",
}


# ============ Helper Functions ============

def calculate_overtime_pay(base_salary: float, hours: float, overtime_type: str) -> float:
    """依勞基法計算加班費"""
    hourly_base = base_salary / 30 / 8

    if overtime_type == "weekday":
        # 平日: 前2小時 1.34x, 後2小時 1.67x
        if hours <= 2:
            return round(hourly_base * hours * 1.34)
        else:
            return round(hourly_base * 2 * 1.34 + hourly_base * (hours - 2) * 1.67)
    else:
        # 假日/國定假日: 全部 2x
        return round(hourly_base * hours * 2)


# ============ Pydantic Models ============

class OvertimeCreate(BaseModel):
    employee_id: int
    overtime_date: date
    overtime_type: str  # weekday / weekend / holiday
    start_time: Optional[str] = None  # HH:MM
    end_time: Optional[str] = None    # HH:MM
    hours: float
    reason: Optional[str] = None


class OvertimeUpdate(BaseModel):
    overtime_date: Optional[date] = None
    overtime_type: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    hours: Optional[float] = None
    reason: Optional[str] = None


# ============ Routes ============

@router.get("/overtimes")
def get_overtimes(
    employee_id: Optional[int] = None,
    year: Optional[int] = None,
    month: Optional[int] = None
):
    """查詢加班記錄"""
    session = get_session()
    try:
        q = session.query(OvertimeRecord, Employee).join(
            Employee, OvertimeRecord.employee_id == Employee.id
        )
        if employee_id:
            q = q.filter(OvertimeRecord.employee_id == employee_id)
        if year and month:
            _, last_day = cal_module.monthrange(year, month)
            start = date(year, month, 1)
            end = date(year, month, last_day)
            q = q.filter(OvertimeRecord.overtime_date >= start, OvertimeRecord.overtime_date <= end)
        elif year:
            q = q.filter(OvertimeRecord.overtime_date >= date(year, 1, 1), OvertimeRecord.overtime_date <= date(year, 12, 31))

        records = q.order_by(OvertimeRecord.overtime_date.desc()).all()

        results = []
        for ot, emp in records:
            results.append({
                "id": ot.id,
                "employee_id": ot.employee_id,
                "employee_name": emp.name,
                "overtime_date": ot.overtime_date.isoformat(),
                "overtime_type": ot.overtime_type,
                "overtime_type_label": OVERTIME_TYPE_LABELS.get(ot.overtime_type, ot.overtime_type),
                "start_time": ot.start_time.strftime("%H:%M") if ot.start_time else None,
                "end_time": ot.end_time.strftime("%H:%M") if ot.end_time else None,
                "hours": ot.hours,
                "overtime_pay": ot.overtime_pay,
                "is_approved": ot.is_approved,
                "approved_by": ot.approved_by,
                "reason": ot.reason,
                "created_at": ot.created_at.isoformat() if ot.created_at else None,
            })
        return results
    finally:
        session.close()


@router.post("/overtimes")
def create_overtime(data: OvertimeCreate):
    """新增加班記錄（自動計算加班費）"""
    session = get_session()
    try:
        if data.overtime_type not in OVERTIME_TYPE_LABELS:
            raise HTTPException(status_code=400, detail=f"無效的加班類型: {data.overtime_type}")

        emp = session.query(Employee).filter(Employee.id == data.employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="員工不存在")

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
            employee_id=data.employee_id,
            overtime_date=data.overtime_date,
            overtime_type=data.overtime_type,
            start_time=start_dt,
            end_time=end_dt,
            hours=data.hours,
            overtime_pay=pay,
            reason=data.reason,
        )
        session.add(ot)
        session.commit()
        return {"message": "加班記錄已新增", "id": ot.id, "overtime_pay": pay}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/overtimes/{overtime_id}")
def update_overtime(overtime_id: int, data: OvertimeUpdate):
    """更新加班記錄"""
    session = get_session()
    try:
        ot = session.query(OvertimeRecord).filter(OvertimeRecord.id == overtime_id).first()
        if not ot:
            raise HTTPException(status_code=404, detail="加班記錄不存在")

        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None and key not in ('start_time', 'end_time'):
                setattr(ot, key, value)

        if data.start_time:
            h, m = map(int, data.start_time.split(":"))
            ot.start_time = datetime.combine(ot.overtime_date, datetime.min.time().replace(hour=h, minute=m))
        if data.end_time:
            h, m = map(int, data.end_time.split(":"))
            ot.end_time = datetime.combine(ot.overtime_date, datetime.min.time().replace(hour=h, minute=m))

        # Recalculate pay
        emp = session.query(Employee).filter(Employee.id == ot.employee_id).first()
        if emp:
            ot.overtime_pay = calculate_overtime_pay(emp.base_salary, ot.hours, ot.overtime_type)

        session.commit()
        return {"message": "加班記錄已更新", "overtime_pay": ot.overtime_pay}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/overtimes/{overtime_id}")
def delete_overtime(overtime_id: int):
    """刪除加班記錄"""
    session = get_session()
    try:
        ot = session.query(OvertimeRecord).filter(OvertimeRecord.id == overtime_id).first()
        if not ot:
            raise HTTPException(status_code=404, detail="加班記錄不存在")
        session.delete(ot)
        session.commit()
        return {"message": "加班記錄已刪除"}
    finally:
        session.close()


@router.put("/overtimes/{overtime_id}/approve")
def approve_overtime(overtime_id: int, approved: bool = True, approved_by: str = "Admin"):
    """核准/駁回加班"""
    session = get_session()
    try:
        ot = session.query(OvertimeRecord).filter(OvertimeRecord.id == overtime_id).first()
        if not ot:
            raise HTTPException(status_code=404, detail="加班記錄不存在")
        ot.is_approved = approved
        ot.approved_by = approved_by if approved else None
        session.commit()
        return {"message": "已核准" if approved else "已駁回"}
    finally:
        session.close()
