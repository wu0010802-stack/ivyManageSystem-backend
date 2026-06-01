"""
月度固定費用登錄 router（園務行政）

月度損益表 Phase 2：admin 在 96 cell 表格（12 月 × 8 category）逐筆登錄
固定支出與舊制勞退準備金。本 router 提供：

- GET    /api/monthly-fixed-costs?year=YYYY            列出整年所有條目
- PUT    /api/monthly-fixed-costs                      單筆 upsert（by year+month+category）
- PUT    /api/monthly-fixed-costs/batch                批次 upsert（前端「儲存全部」用）
- DELETE /api/monthly-fixed-costs/{id}                 刪除單筆

所有寫操作：
1) audit 透過 `request.state.audit_entity_id` + `audit_summary` 留軌跡
   （AuditMiddleware 接手，須先在 ENTITY_PATTERNS 加 monthly_fixed_cost 條目）
2) 寫入後同步呼叫 `invalidate_finance_summary_cache()` 失效
   `reports_finance_summary` 與 `reports_monthly_pnl` 兩快取
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from models.database import MonthlyFixedCost, get_session
from models.monthly_fixed_cost import FIXED_COST_CATEGORIES
from schemas._common import DeleteResultOut
from utils.auth import require_staff_permission
from utils.errors import raise_safe_500
from utils.finance_cache import invalidate_finance_summary_cache
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["monthly-fixed-costs"])


# Literal type 與 model 的 CheckConstraint 同步維護
CategoryLiteral = Literal[
    "rent",
    "office_petty_cash",
    "kitchen_petty_cash",
    "meals",
    "water",
    "electricity",
    "phone",
    "old_pension_reserve",
]


# ─── Pydantic schemas ────────────────────────────────────────────────────
class MonthlyFixedCostUpsert(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    month: int = Field(..., ge=1, le=12)
    category: CategoryLiteral
    amount: Decimal = Field(..., ge=0, max_digits=12, decimal_places=2)
    notes: Optional[str] = None

    @field_validator("notes", mode="before")
    @classmethod
    def empty_to_none(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v


class MonthlyFixedCostBatchEntry(BaseModel):
    month: int = Field(..., ge=1, le=12)
    category: CategoryLiteral
    amount: Decimal = Field(..., ge=0, max_digits=12, decimal_places=2)
    notes: Optional[str] = None

    @field_validator("notes", mode="before")
    @classmethod
    def empty_to_none(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v


class MonthlyFixedCostBatchUpsert(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    entries: List[MonthlyFixedCostBatchEntry] = Field(..., min_length=1, max_length=200)


# ─── Helpers ─────────────────────────────────────────────────────────────
def _to_dict(row: MonthlyFixedCost) -> dict:
    return {
        "id": row.id,
        "year": row.year,
        "month": row.month,
        "category": row.category,
        "amount": float(row.amount) if row.amount is not None else 0.0,
        "notes": row.notes,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "created_by_id": row.created_by_id,
        "updated_by_id": row.updated_by_id,
    }


def _upsert_one(
    session,
    *,
    year: int,
    month: int,
    category: str,
    amount: Decimal,
    notes: Optional[str],
    user_id: Optional[int],
) -> MonthlyFixedCost:
    """單筆 upsert by (year, month, category)。回傳 ORM row（caller commit）。"""
    row = (
        session.query(MonthlyFixedCost)
        .filter(
            MonthlyFixedCost.year == year,
            MonthlyFixedCost.month == month,
            MonthlyFixedCost.category == category,
        )
        .first()
    )
    if row is None:
        row = MonthlyFixedCost(
            year=year,
            month=month,
            category=category,
            amount=amount,
            notes=notes,
            created_by_id=user_id,
            updated_by_id=user_id,
        )
        session.add(row)
    else:
        row.amount = amount
        row.notes = notes
        row.updated_by_id = user_id
    session.flush()
    return row


# ─── Endpoints ───────────────────────────────────────────────────────────
@router.get("/monthly-fixed-costs")
def list_monthly_fixed_costs(
    year: int = Query(..., ge=2000, le=2100),
    current_user: dict = Depends(
        require_staff_permission(Permission.VENDOR_PAYMENT_READ)
    ),
):
    """列出指定年度所有月度固定費用條目（依 month、category 排序）。

    回傳前端用於組裝 12×8 試算表；未登錄的 (month, category) 不會出現在 list 中，
    前端自行補成 0/empty input。
    """
    session = get_session()
    try:
        rows = (
            session.query(MonthlyFixedCost)
            .filter(MonthlyFixedCost.year == year)
            .order_by(MonthlyFixedCost.month, MonthlyFixedCost.category)
            .all()
        )
        return {
            "year": year,
            "items": [_to_dict(r) for r in rows],
            "valid_categories": list(FIXED_COST_CATEGORIES),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="列表查詢失敗")
    finally:
        session.close()


@router.put("/monthly-fixed-costs")
def upsert_monthly_fixed_cost(
    payload: MonthlyFixedCostUpsert,
    request: Request,
    current_user: dict = Depends(
        require_staff_permission(Permission.VENDOR_PAYMENT_WRITE)
    ),
):
    """單筆 upsert by (year, month, category)。"""
    session = get_session()
    try:
        row = _upsert_one(
            session,
            year=payload.year,
            month=payload.month,
            category=payload.category,
            amount=payload.amount,
            notes=payload.notes,
            user_id=current_user.get("user_id"),
        )
        session.commit()
        session.refresh(row)
        invalidate_finance_summary_cache()

        request.state.audit_entity_id = str(row.id)
        request.state.audit_summary = (
            f"更新月度固定費用 {payload.year}-{payload.month:02d} "
            f"{payload.category} = {payload.amount}"
        )
        return {"message": "更新成功", "id": row.id, "item": _to_dict(row)}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("upsert 月度固定費用失敗")
        raise_safe_500(e, context="更新失敗")
    finally:
        session.close()


@router.put("/monthly-fixed-costs/batch")
def batch_upsert_monthly_fixed_costs(
    payload: MonthlyFixedCostBatchUpsert,
    request: Request,
    current_user: dict = Depends(
        require_staff_permission(Permission.VENDOR_PAYMENT_WRITE)
    ),
):
    """批次 upsert 整年某幾筆條目。整批 atomic，任何一筆失敗整批回滾。

    前端「儲存全部」按鈕只送 dirty 條目，避免無謂寫入。重複 (month, category)
    視為錯誤（同一鍵不應送兩次）。
    """
    # 提前驗證：同一 batch 內 (month, category) 不可重複
    seen: set[tuple[int, str]] = set()
    for entry in payload.entries:
        key = (entry.month, entry.category)
        if key in seen:
            raise HTTPException(
                status_code=400,
                detail=f"批次中 (month={entry.month}, category={entry.category}) 重複",
            )
        seen.add(key)

    session = get_session()
    try:
        user_id = current_user.get("user_id")
        ids: list[int] = []
        for entry in payload.entries:
            row = _upsert_one(
                session,
                year=payload.year,
                month=entry.month,
                category=entry.category,
                amount=entry.amount,
                notes=entry.notes,
                user_id=user_id,
            )
            ids.append(row.id)
        session.commit()
        invalidate_finance_summary_cache()

        request.state.audit_entity_id = str(payload.year)
        request.state.audit_summary = (
            f"批次更新月度固定費用 {payload.year} 共 {len(payload.entries)} 筆"
        )
        return {
            "message": "批次更新成功",
            "year": payload.year,
            "count": len(ids),
            "ids": ids,
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logger.exception("批次 upsert 月度固定費用失敗")
        raise_safe_500(e, context="批次更新失敗")
    finally:
        session.close()


@router.delete("/monthly-fixed-costs/{cost_id}", response_model=DeleteResultOut)
def delete_monthly_fixed_cost(
    cost_id: int,
    request: Request,
    current_user: dict = Depends(
        require_staff_permission(Permission.VENDOR_PAYMENT_WRITE)
    ),
):
    """刪除單筆月度固定費用。"""
    session = get_session()
    try:
        row = (
            session.query(MonthlyFixedCost)
            .filter(MonthlyFixedCost.id == cost_id)
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="月度固定費用紀錄不存在")
        snapshot = (row.year, row.month, row.category, float(row.amount or 0))
        session.delete(row)
        session.commit()
        invalidate_finance_summary_cache()

        request.state.audit_entity_id = str(cost_id)
        request.state.audit_summary = (
            f"刪除月度固定費用 {snapshot[0]}-{snapshot[1]:02d} "
            f"{snapshot[2]} (原金額={snapshot[3]})"
        )
        return {"message": "刪除成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("刪除月度固定費用失敗")
        raise_safe_500(e, context="刪除失敗")
    finally:
        session.close()
