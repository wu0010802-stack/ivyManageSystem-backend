"""
Employee allowance management router
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from utils.auth import require_permission
from utils.permissions import Permission
from pydantic import BaseModel

from models.database import get_session, EmployeeAllowance, AllowanceType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["employee-allowances"])


# ============ Pydantic Models ============

class EmployeeAllowanceCreate(BaseModel):
    allowance_type_id: int
    amount: float
    effective_date: Optional[str] = None
    remark: Optional[str] = None


class EmployeeAllowanceUpdate(BaseModel):
    amount: Optional[float] = None
    effective_date: Optional[str] = None
    remark: Optional[str] = None


# ============ Routes ============

@router.get("/employees/{employee_id}/allowances")
async def get_employee_allowances(employee_id: int, current_user: dict = Depends(require_permission(Permission.SALARY))):
    session = get_session()
    try:
        allowances = session.query(EmployeeAllowance, AllowanceType).join(AllowanceType).filter(
            EmployeeAllowance.employee_id == employee_id,
            EmployeeAllowance.is_active == True
        ).all()

        return [{
            "id": ea.id,
            "allowance_type_id": at.id,
            "name": at.name,
            "amount": ea.amount,
            "effective_date": ea.effective_date,
            "remark": ea.remark
        } for ea, at in allowances]
    finally:
        session.close()


@router.post("/employees/{employee_id}/allowances", status_code=201)
async def add_employee_allowance(employee_id: int, data: EmployeeAllowanceCreate, current_user: dict = Depends(require_permission(Permission.SALARY))):
    session = get_session()
    try:
        # 簡單處理：如果已存在相同類型則更新，否則新增
        existing = session.query(EmployeeAllowance).filter(
            EmployeeAllowance.employee_id == employee_id,
            EmployeeAllowance.allowance_type_id == data.allowance_type_id,
            EmployeeAllowance.is_active == True
        ).first()

        if existing:
            existing.amount = data.amount
            existing.effective_date = data.effective_date
            existing.remark = data.remark
        else:
            new_allowance = EmployeeAllowance(
                employee_id=employee_id,
                **data.dict()
            )
            session.add(new_allowance)

        session.commit()
        return {"message": "儲存成功"}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/employees/{employee_id}/allowances/{allowance_id}")
async def update_employee_allowance(
    employee_id: int,
    allowance_id: int,
    data: EmployeeAllowanceUpdate,
    current_user: dict = Depends(require_permission(Permission.SALARY)),
):
    """更新員工津貼"""
    session = get_session()
    try:
        allowance = session.query(EmployeeAllowance).filter(
            EmployeeAllowance.id == allowance_id,
            EmployeeAllowance.employee_id == employee_id,
            EmployeeAllowance.is_active == True,
        ).first()
        if not allowance:
            raise HTTPException(status_code=404, detail="找不到該津貼記錄")

        if data.amount is not None:
            allowance.amount = data.amount
        if data.effective_date is not None:
            allowance.effective_date = data.effective_date
        if data.remark is not None:
            allowance.remark = data.remark

        session.commit()
        return {"message": "更新成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/employees/{employee_id}/allowances/{allowance_id}")
async def delete_employee_allowance(
    employee_id: int,
    allowance_id: int,
    current_user: dict = Depends(require_permission(Permission.SALARY)),
):
    """刪除員工津貼（軟刪除）"""
    session = get_session()
    try:
        allowance = session.query(EmployeeAllowance).filter(
            EmployeeAllowance.id == allowance_id,
            EmployeeAllowance.employee_id == employee_id,
            EmployeeAllowance.is_active == True,
        ).first()
        if not allowance:
            raise HTTPException(status_code=404, detail="找不到該津貼記錄")

        allowance.is_active = False
        session.commit()
        return {"message": "已刪除"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()
