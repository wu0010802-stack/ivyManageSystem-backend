"""
Insurance router
"""

import logging
from typing import List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["insurance"])


# ============ Service Init ============

_insurance_service = None


def init_insurance_services(insurance_service):
    global _insurance_service
    _insurance_service = insurance_service


# ============ Pydantic Models ============

class InsuranceTableImport(BaseModel):
    table_type: str = "labor"
    data: List[dict]


# ============ Routes ============

@router.post("/insurance/import")
async def import_insurance_table(data: InsuranceTableImport):
    """匯入勞健保級距表"""
    success = _insurance_service.import_table(data=data.data, table_type=data.table_type)
    if success:
        return {"message": f"{data.table_type} 級距表匯入成功"}
    raise HTTPException(status_code=400, detail="匯入失敗")


@router.get("/insurance/calculate")
async def calculate_insurance(salary: float = Query(...), dependents: int = Query(0)):
    """計算勞健保"""
    result = _insurance_service.calculate(salary, dependents)
    return {
        "insured_amount": result.insured_amount,
        "labor_employee": result.labor_employee,
        "labor_employer": result.labor_employer,
        "health_employee": result.health_employee,
        "health_employer": result.health_employer,
        "pension_employer": result.pension_employer,
        "total_employee": result.total_employee,
        "total_employer": result.total_employer
    }
