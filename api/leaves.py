"""
Leave management router
"""

import logging
import calendar as cal_module
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from models.database import get_session, Employee, LeaveRecord
from utils.auth import get_current_user, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["leaves"])


# ============ Constants ============

# 請假扣薪規則（依勞基法）
LEAVE_DEDUCTION_RULES = {
    "personal": 1.0,   # 事假: 全扣
    "sick": 0.5,        # 病假: 扣半薪
    "menstrual": 0.5,   # 生理假: 扣半薪
    "annual": 0.0,      # 特休: 不扣
    "maternity": 0.0,   # 產假: 不扣
    "paternity": 0.0,   # 陪產假: 不扣
}

LEAVE_TYPE_LABELS = {
    "personal": "事假",
    "sick": "病假",
    "menstrual": "生理假",
    "annual": "特休",
    "maternity": "產假",
    "paternity": "陪產假",
}


# ============ Pydantic Models ============

class LeaveCreate(BaseModel):
    employee_id: int
    leave_type: str
    start_date: date
    end_date: date
    leave_hours: float = 8
    reason: Optional[str] = None


class LeaveUpdate(BaseModel):
    leave_type: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    leave_hours: Optional[float] = None
    reason: Optional[str] = None


# ============ Routes ============

@router.get("/leaves")
def get_leaves(
    employee_id: Optional[int] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """查詢請假記錄"""
    session = get_session()
    try:
        q = session.query(LeaveRecord, Employee).join(
            Employee, LeaveRecord.employee_id == Employee.id
        )
        if employee_id:
            q = q.filter(LeaveRecord.employee_id == employee_id)
        if status == "pending":
            q = q.filter(LeaveRecord.is_approved.is_(None))
        elif status == "approved":
            q = q.filter(LeaveRecord.is_approved == True)
        elif status == "rejected":
            q = q.filter(LeaveRecord.is_approved == False)
        if year and month:
            _, last_day = cal_module.monthrange(year, month)
            start = date(year, month, 1)
            end = date(year, month, last_day)
            q = q.filter(LeaveRecord.start_date <= end, LeaveRecord.end_date >= start)
        elif year:
            q = q.filter(LeaveRecord.start_date >= date(year, 1, 1), LeaveRecord.start_date <= date(year, 12, 31))

        records = q.order_by(LeaveRecord.start_date.desc()).all()

        results = []
        for leave, emp in records:
            results.append({
                "id": leave.id,
                "employee_id": leave.employee_id,
                "employee_name": emp.name,
                "leave_type": leave.leave_type,
                "leave_type_label": LEAVE_TYPE_LABELS.get(leave.leave_type, leave.leave_type),
                "start_date": leave.start_date.isoformat(),
                "end_date": leave.end_date.isoformat(),
                "leave_hours": leave.leave_hours,
                "deduction_ratio": LEAVE_DEDUCTION_RULES.get(leave.leave_type, 1.0),
                "reason": leave.reason,
                "is_approved": leave.is_approved,
                "approved_by": leave.approved_by,
                "created_at": leave.created_at.isoformat() if leave.created_at else None,
            })
        return results
    finally:
        session.close()


@router.post("/leaves")
def create_leave(data: LeaveCreate, current_user: dict = Depends(require_admin)):
    """新增請假記錄"""
    session = get_session()
    try:
        if data.leave_type not in LEAVE_DEDUCTION_RULES:
            raise HTTPException(status_code=400, detail=f"無效的假別: {data.leave_type}")

        emp = session.query(Employee).filter(Employee.id == data.employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="員工不存在")

        leave = LeaveRecord(
            employee_id=data.employee_id,
            leave_type=data.leave_type,
            start_date=data.start_date,
            end_date=data.end_date,
            leave_hours=data.leave_hours,
            is_deductible=LEAVE_DEDUCTION_RULES[data.leave_type] > 0,
            deduction_ratio=LEAVE_DEDUCTION_RULES[data.leave_type],
            reason=data.reason,
        )
        session.add(leave)
        session.commit()
        return {"message": "請假記錄已新增", "id": leave.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/leaves/{leave_id}")
def update_leave(leave_id: int, data: LeaveUpdate, current_user: dict = Depends(require_admin)):
    """更新請假記錄"""
    session = get_session()
    try:
        leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first()
        if not leave:
            raise HTTPException(status_code=404, detail="請假記錄不存在")

        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None:
                setattr(leave, key, value)
        # Update deduction fields if leave_type changed
        if data.leave_type and data.leave_type in LEAVE_DEDUCTION_RULES:
            leave.is_deductible = LEAVE_DEDUCTION_RULES[data.leave_type] > 0
            leave.deduction_ratio = LEAVE_DEDUCTION_RULES[data.leave_type]

        session.commit()
        return {"message": "請假記錄已更新"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/leaves/{leave_id}")
def delete_leave(leave_id: int, current_user: dict = Depends(require_admin)):
    """刪除請假記錄"""
    session = get_session()
    try:
        leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first()
        if not leave:
            raise HTTPException(status_code=404, detail="請假記錄不存在")
        session.delete(leave)
        session.commit()
        return {"message": "請假記錄已刪除"}
    finally:
        session.close()


@router.put("/leaves/{leave_id}/approve")
def approve_leave(leave_id: int, approved: bool = True, approved_by: str = "管理員", current_user: dict = Depends(require_admin)):
    """核准/駁回請假"""
    session = get_session()
    try:
        leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).first()
        if not leave:
            raise HTTPException(status_code=404, detail="請假記錄不存在")
        leave.is_approved = approved
        leave.approved_by = approved_by if approved else None
        session.commit()
        return {"message": "已核准" if approved else "已駁回"}
    finally:
        session.close()
