"""
Insurance router
"""

import logging
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.finance_guards import has_finance_approve, require_adjustment_reason
from pydantic import BaseModel, Field

from models.database import get_session, session_scope, InsuranceBracket, SalaryRecord

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


class InsuranceBracketIn(BaseModel):
    """單筆級距資料；amount 為投保金額，其他欄位為政府公告之員雇/勞退金額。"""

    amount: int = Field(gt=0, description="投保金額（必須 > 0）")
    labor_employee: int = Field(ge=0)
    labor_employer: int = Field(ge=0)
    health_employee: int = Field(ge=0)
    health_employer: int = Field(ge=0)
    pension: int = Field(ge=0)


class InsuranceBracketsBulkUpsert(BaseModel):
    effective_year: int = Field(
        ge=2020,
        le=2100,
        description="適用年度（西元，與 InsuranceRate.rate_year 對齊）",
    )
    brackets: List[InsuranceBracketIn] = Field(min_length=1)
    replace_existing: bool = Field(
        default=False,
        description="True=先刪除該年度所有列再寫入；False=以 (year, amount) UPSERT",
    )
    reason: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description="變更原因（≥10 字），落 audit；級距表異動牽動全員保費",
    )


class InsuranceBracketDeleteRequest(BaseModel):
    reason: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description="刪除原因（≥10 字），落 audit；級距表異動牽動全員保費",
    )


def _bulk_mark_salary_stale_for_year(session, salary_year: int) -> int:
    """把指定西元年份所有未封存的 SalaryRecord 標 needs_recalc=True。

    Why: 勞健保級距表異動會改變所有員工的 labor/health/pension 計算。
    若不標 stale，已算未封存的薪資仍 needs_recalc=False，下次 finalize 會用
    新（可能被惡意調低）級距值落帳，且因 _select_active_at 重讀 brackets/
    InsuranceRate，歷史月份補算也跟著漂。本 helper 對該年所有未封存
    SalaryRecord 標 stale，強制 finalize 前重算。

    Refs: 邏輯漏洞 audit 2026-05-07 P0 (#9)。
    """
    affected = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.salary_year == salary_year,
            SalaryRecord.is_finalized.is_(False),
        )
        .update({"needs_recalc": True}, synchronize_session=False)
    )
    return affected


# ============ Routes ============


@router.post("/insurance/import")
async def import_insurance_table(
    data: InsuranceTableImport,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """匯入勞健保級距表"""
    success = _insurance_service.import_table(
        data=data.data, table_type=data.table_type
    )
    if success:
        return {"message": f"{data.table_type} 級距表匯入成功"}
    raise HTTPException(status_code=400, detail="匯入失敗")


@router.get("/insurance/calculate")
async def calculate_insurance(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    salary: float = Query(...),
    dependents: int = Query(0),
):
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
        "total_employer": result.total_employer,
    }


# ============ 級距表維護 (admin) ============


@router.get("/insurance/brackets")
async def list_brackets(
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_READ)),
    year: Optional[int] = Query(None, description="預設取當年；可指定歷史年度"),
):
    """讀取指定年度的勞健保級距表。

    無資料時 fallback 到 ≤year 中最新的年度（與 service load_brackets_from_db 同邏輯），
    便於行政在新年度尚未公告時還能看到目前生效級距。
    """
    target_year = year or date.today().year
    session = get_session()
    try:
        rows = (
            session.query(InsuranceBracket)
            .filter(InsuranceBracket.effective_year == target_year)
            .order_by(InsuranceBracket.amount.asc())
            .all()
        )
        actual_year = target_year
        if not rows:
            fallback = (
                session.query(InsuranceBracket.effective_year)
                .filter(InsuranceBracket.effective_year <= target_year)
                .order_by(InsuranceBracket.effective_year.desc())
                .first()
            )
            if fallback:
                actual_year = fallback[0]
                rows = (
                    session.query(InsuranceBracket)
                    .filter(InsuranceBracket.effective_year == actual_year)
                    .order_by(InsuranceBracket.amount.asc())
                    .all()
                )
        return {
            "requested_year": target_year,
            "effective_year": actual_year if rows else None,
            "brackets": [
                {
                    "id": r.id,
                    "amount": r.amount,
                    "labor_employee": r.labor_employee,
                    "labor_employer": r.labor_employer,
                    "health_employee": r.health_employee,
                    "health_employer": r.health_employer,
                    "pension": r.pension,
                }
                for r in rows
            ],
        }
    finally:
        session.close()


@router.put("/insurance/brackets")
async def upsert_brackets(
    payload: InsuranceBracketsBulkUpsert,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """新增/更新指定年度的整張級距表。

    用法（每年公告新分級表）：行政上傳 effective_year=新年度 + 全部級距列；
    `replace_existing=True` 適合「整張表重整」，否則以 (year, amount) UPSERT。

    寫入後立即呼叫 service.load_brackets_from_db() 讓計算端立刻反映新資料，
    避免要重啟 server 才生效（與 BonusConfig PATCH 流程一致）。

    守衛（audit 2026-05-07 P0 #9）：
    - 雖只需 SALARY_WRITE 即可呼叫（級距表為政府公告，常規業務動作），但
      變更會影響全員保費；額外要求 has_finance_approve（即 ACTIVITY_PAYMENT_APPROVE）
      或符合「正規行政上傳」流程；reason 必填 ≥10 字落 audit。
    - 寫入成功後對該 effective_year 所有未封存 SalaryRecord 標 needs_recalc=True，
      避免「stale 未標 → finalize 用新（可能被改低）級距值落帳」的攻擊。
    """
    if not has_finance_approve(current_user):
        raise HTTPException(
            status_code=403,
            detail=(
                "勞健保級距表變更影響全員保費，需由具備『金流簽核』權限者"
                "（ACTIVITY_PAYMENT_APPROVE）執行"
            ),
        )
    cleaned_reason = require_adjustment_reason(payload.reason)

    with session_scope() as session:
        if payload.replace_existing:
            session.query(InsuranceBracket).filter(
                InsuranceBracket.effective_year == payload.effective_year
            ).delete()
            session.flush()

        existing = {
            r.amount: r
            for r in session.query(InsuranceBracket)
            .filter(InsuranceBracket.effective_year == payload.effective_year)
            .all()
        }
        upserted = 0
        for b in payload.brackets:
            row = existing.get(b.amount)
            if row is None:
                session.add(
                    InsuranceBracket(
                        effective_year=payload.effective_year,
                        amount=b.amount,
                        labor_employee=b.labor_employee,
                        labor_employer=b.labor_employer,
                        health_employee=b.health_employee,
                        health_employer=b.health_employer,
                        pension=b.pension,
                    )
                )
            else:
                row.labor_employee = b.labor_employee
                row.labor_employer = b.labor_employer
                row.health_employee = b.health_employee
                row.health_employer = b.health_employer
                row.pension = b.pension
            upserted += 1

        # 級距表異動 → 該年所有未封存薪資全部標 stale
        stale_marked = _bulk_mark_salary_stale_for_year(session, payload.effective_year)

    # session_scope 已 commit；reload 放 with 外，確保 service 看到最新狀態
    if _insurance_service is not None:
        _insurance_service.load_brackets_from_db(payload.effective_year)

    logger.warning(
        "勞健保級距表變更：effective_year=%s, upserted=%d, replaced_existing=%s, "
        "stale_marked=%d, by=%s, reason=%s",
        payload.effective_year,
        upserted,
        payload.replace_existing,
        stale_marked,
        current_user.get("username"),
        cleaned_reason,
    )

    return {
        "message": "級距表已更新",
        "effective_year": payload.effective_year,
        "upserted": upserted,
        "replaced_existing": payload.replace_existing,
        "stale_marked": stale_marked,
    }


@router.delete("/insurance/brackets/{bracket_id}")
async def delete_bracket(
    bracket_id: int,
    payload: InsuranceBracketDeleteRequest,
    current_user: dict = Depends(require_staff_permission(Permission.SALARY_WRITE)),
):
    """刪除單一級距列（行政誤新增時的修正用）。

    刪除後立即同步 service 的 in-memory 級距表（與 PUT /brackets 行為一致），
    避免要重啟 server 才生效。

    守衛（audit 2026-05-07 P0 #9）：與 PUT 對齊，刪一筆級距會讓該級距金額落入
    fallback 邏輯，足以低估保費，故同樣要求 finance_approve + reason ≥10 字 +
    bulk mark stale。
    """
    if not has_finance_approve(current_user):
        raise HTTPException(
            status_code=403,
            detail=(
                "勞健保級距表變更影響全員保費，需由具備『金流簽核』權限者"
                "（ACTIVITY_PAYMENT_APPROVE）執行"
            ),
        )
    cleaned_reason = require_adjustment_reason(payload.reason)

    with session_scope() as session:
        row = session.query(InsuranceBracket).get(bracket_id)
        if row is None:
            raise HTTPException(status_code=404, detail="級距不存在")
        year = row.effective_year
        amount = row.amount
        session.delete(row)
        stale_marked = _bulk_mark_salary_stale_for_year(session, year)

    if _insurance_service is not None:
        _insurance_service.load_brackets_from_db(year)

    logger.warning(
        "勞健保級距列刪除：bracket_id=%d, effective_year=%s, amount=%s, "
        "stale_marked=%d, by=%s, reason=%s",
        bracket_id,
        year,
        amount,
        stale_marked,
        current_user.get("username"),
        cleaned_reason,
    )
    return {"message": "已刪除", "effective_year": year, "stale_marked": stale_marked}
