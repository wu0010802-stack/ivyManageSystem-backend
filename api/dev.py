"""開發用 API - 檢視薪資計算邏輯、出缺勤規則、系統設定。

正式頁面已改打 `/api/salaries/logic` 與 `/api/salaries/employee-salary-debug`，
此處保留 `/api/dev/*` 端點僅作為開發/測試環境的舊 URL 相容（main.py 會視 ENV
白名單決定是否掛載），全部委派給共享 service / 既有 helper。
"""

import logging

from fastapi import APIRouter, Depends, Query

from models.database import Employee, get_session
from services.salary_field_breakdown import build_salary_debug_snapshot
from services.salary_logic_info import build_salary_logic_info
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dev", tags=["dev"])

_salary_engine = None


def init_dev_services(salary_engine):
    global _salary_engine
    _salary_engine = salary_engine


@router.get("/salary-logic")
def get_salary_logic(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    """傾印目前的薪資計算邏輯與所有參數設定（dev 別名，正式請改打 /api/salaries/logic）。"""
    session = get_session()
    try:
        return build_salary_logic_info(session, _salary_engine)
    finally:
        session.close()


@router.get("/employee-salary-debug")
def debug_employee_salary(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
    employee_id: int = Query(...),
    year: int = Query(...),
    month: int = Query(...),
):
    """模擬計算單一員工薪資並回傳完整明細（dev 別名，正式請改打 /api/salaries/employee-salary-debug）。"""
    session = get_session()
    try:
        engine = _salary_engine
        if not engine:
            return {"error": "SalaryEngine not initialized"}

        emp = session.query(Employee).get(employee_id)
        if not emp:
            return {"error": f"Employee {employee_id} not found"}

        if emp.employee_type == "hourly":
            return {
                "error": "時薪制員工請使用正式薪資計算流程，debug 端點僅支援月薪正職員工"
            }
        return build_salary_debug_snapshot(session, engine, emp, year, month)
    finally:
        session.close()
