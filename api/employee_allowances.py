"""
Employee allowance management router
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from utils.errors import raise_safe_500
from utils.auth import require_permission
from utils.permissions import Permission
from pydantic import BaseModel, Field

from models.database import get_session, EmployeeAllowance, AllowanceType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["employee-allowances"])


def _clear_allowance_cache():
    """清除 salary.py 中的津貼快取，確保下次薪資計算取到最新資料。"""
    try:
        from api.salary import _allowance_cache
        _allowance_cache.clear()
    except Exception as e:
        logger.debug("清除津貼快取失敗（可忽略）: %s", e)


# ============ Pydantic Models ============

class EmployeeAllowanceCreate(BaseModel):
    allowance_type_id: int
    amount: float = Field(..., ge=0)
    effective_date: Optional[str] = None
    remark: Optional[str] = None


class EmployeeAllowanceUpdate(BaseModel):
    amount: Optional[float] = Field(None, ge=0)
    effective_date: Optional[str] = None
    remark: Optional[str] = None


# ============ Routes ============

@router.get("/employees/{employee_id}/allowances")
async def get_employee_allowances(employee_id: int, current_user: dict = Depends(require_permission(Permission.SALARY_READ))):
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
async def add_employee_allowance(employee_id: int, data: EmployeeAllowanceCreate, current_user: dict = Depends(require_permission(Permission.SALARY_WRITE))):
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
                **data.model_dump()
            )
            session.add(new_allowance)

        session.commit()
        _clear_allowance_cache()
        return {"message": "儲存成功"}
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.put("/employees/{employee_id}/allowances/{allowance_id}")
async def update_employee_allowance(
    employee_id: int,
    allowance_id: int,
    data: EmployeeAllowanceUpdate,
    current_user: dict = Depends(require_permission(Permission.SALARY_WRITE)),
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
        _clear_allowance_cache()
        return {"message": "更新成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/employees/{employee_id}/allowances/{allowance_id}")
async def delete_employee_allowance(
    employee_id: int,
    allowance_id: int,
    current_user: dict = Depends(require_permission(Permission.SALARY_WRITE)),
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
        _clear_allowance_cache()
        return {"message": "已刪除"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()
