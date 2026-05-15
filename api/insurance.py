"""
Insurance router
"""

import logging
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.finance_guards import has_finance_approve, require_adjustment_reason
from pydantic import BaseModel, Field

from models.database import (
    get_session,
    session_scope,
    InsuranceBracket,
    InsuranceBracketsStaging,
    SalaryRecord,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["insurance"])


# ============ Service Init ============

_insurance_service = None


def init_insurance_services(insurance_service):
    global _insurance_service
    _insurance_service = insurance_service


# 模組層級 dependency 常數，方便測試以 app.dependency_overrides 覆寫
_DEP_SALARY_READ = require_staff_permission(Permission.SALARY_READ)
_DEP_SALARY_WRITE = require_staff_permission(Permission.SALARY_WRITE)


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
    # 資安掃描 2026-05-07 P2：當該年度已有封存月份時，預設拒絕改動。
    # 二次審批：行政人員確認影響後重送 acknowledge_finalized_months=True 才放行。
    # 已封存月份不會被重算（is_finalized=False 才標 stale），但中途異動會讓
    # 半年報表跨段使用不同級距值，需業主明確同意。
    acknowledge_finalized_months: bool = Field(
        default=False,
        description="該年度已有封存薪資月份時必須帶 True 才放行（二次審批）",
    )


class InsuranceBracketDeleteRequest(BaseModel):
    reason: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description="刪除原因（≥10 字），落 audit；級距表異動牽動全員保費",
    )
    acknowledge_finalized_months: bool = Field(
        default=False,
        description="該年度已有封存薪資月份時必須帶 True 才放行（二次審批）",
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


def _count_finalized_months_for_year(session, salary_year: int) -> int:
    """數該年度已封存的（distinct）薪資月份數。

    Why: 資安掃描 2026-05-07 P2 — 已封存月份不會被本次 stale 重算覆蓋，
    但若同年中途改級距會造成半年報表跨段異質。改動時需業主二次確認。
    """
    from sqlalchemy import distinct, func

    return (
        session.query(func.count(distinct(SalaryRecord.salary_month)))
        .filter(
            SalaryRecord.salary_year == salary_year,
            SalaryRecord.is_finalized.is_(True),
        )
        .scalar()
        or 0
    )


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
    # eval framework 揭露:service 對 NaN salary / 負薪資 / 越界 dependents
    # 等都 raise ValueError;原 endpoint 沒 catch 會漏成 500。
    try:
        result = _insurance_service.calculate(salary, dependents)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
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
    current_user: dict = Depends(_DEP_SALARY_READ),
    year: Optional[int] = Query(None, description="預設取當年；可指定歷史年度"),
):
    """讀取指定年度的勞健保級距表。

    無資料時 fallback 到 ≤year 中最新的年度（與 service load_brackets_from_db 同邏輯），
    便於行政在新年度尚未公告時還能看到目前生效級距。

    回應包含 `latest_promoted_from_gov_data` 旗標，表示該年度的級距是否由政府資料 promote 流程寫入。
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
        # 判斷 effective_year 的資料是否來自政府資料 promote 流程
        promoted_staging = (
            session.query(InsuranceBracketsStaging)
            .filter(
                InsuranceBracketsStaging.effective_year == actual_year,
                InsuranceBracketsStaging.status == "promoted",
            )
            .first()
        )
        latest_promoted_from_gov_data = promoted_staging is not None
        return {
            "requested_year": target_year,
            "effective_year": actual_year if rows else None,
            "latest_promoted_from_gov_data": latest_promoted_from_gov_data,
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
    request: Request,
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
        # 資安掃描 2026-05-07 P2：該年度已有封存月份時要求二次確認
        finalized_months = _count_finalized_months_for_year(
            session, payload.effective_year
        )
        if finalized_months > 0 and not payload.acknowledge_finalized_months:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"該年度（{payload.effective_year}）已有 {finalized_months} 個"
                    f"月份封存。封存月份不會被重算，但同年中途改級距會讓半年/年度"
                    f"報表跨段不一致。如確認影響，請帶 acknowledge_finalized_months=True 重送。"
                ),
            )

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

    # session_scope 已 commit；reload 放 with 外，確保 service 看到最新狀態。
    # strict=True：admin 已寫入 DB，若 reload 失敗應 surface 5xx 讓前端知道，
    # 避免管理員看到「儲存成功」但計算仍走舊 hardcode。
    if _insurance_service is not None:
        try:
            _insurance_service.load_brackets_from_db(
                payload.effective_year, strict=True
            )
        except Exception as e:
            logger.error("勞健保級距 reload 失敗", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=(
                    "級距已寫入 DB，但 service reload 失敗："
                    f"{type(e).__name__}: {e}。"
                    "請聯絡 ops 確認；目前計算端仍使用 reload 前的級距表。"
                ),
            )

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

    # AuditMiddleware 會把 entity_type=insurance_bracket / action=UPDATE 落 audit_logs
    # changes 帶 effective_year / 上傳列數 / replace flag / 觸發 stale 數 / reason，
    # 事後溯源用 audit-logs 篩 entity_type=insurance_bracket 即可看到完整異動軌跡。
    request.state.audit_changes = {
        "effective_year": payload.effective_year,
        "upserted": upserted,
        "replaced_existing": payload.replace_existing,
        "stale_marked": stale_marked,
        "finalized_months_in_year": finalized_months,
        "acknowledged_finalized": payload.acknowledge_finalized_months,
        "reason": cleaned_reason,
    }
    request.state.audit_entity_id = payload.effective_year

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
    request: Request,
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
        # 資安掃描 2026-05-07 P2：該年度已有封存月份時要求二次確認
        finalized_months = _count_finalized_months_for_year(session, year)
        if finalized_months > 0 and not payload.acknowledge_finalized_months:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"該年度（{year}）已有 {finalized_months} 個月份封存。"
                    f"封存月份不會被重算，但同年中途刪級距會讓對應投保金額落入"
                    f"fallback 區間。如確認影響，請帶 acknowledge_finalized_months=True 重送。"
                ),
            )
        session.delete(row)
        stale_marked = _bulk_mark_salary_stale_for_year(session, year)

    if _insurance_service is not None:
        try:
            _insurance_service.load_brackets_from_db(year, strict=True)
        except Exception as e:
            logger.error("勞健保級距 reload 失敗（delete 後）", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=(
                    "級距已刪除，但 service reload 失敗："
                    f"{type(e).__name__}: {e}。"
                    "請聯絡 ops 確認；目前計算端仍使用 reload 前的級距表。"
                ),
            )

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
    # AuditMiddleware 會把 entity_type=insurance_bracket / action=DELETE 落 audit_logs
    request.state.audit_changes = {
        "effective_year": year,
        "amount": amount,
        "stale_marked": stale_marked,
        "finalized_months_in_year": finalized_months,
        "acknowledged_finalized": payload.acknowledge_finalized_months,
        "reason": cleaned_reason,
    }
    request.state.audit_entity_id = bracket_id
    return {"message": "已刪除", "effective_year": year, "stale_marked": stale_marked}
