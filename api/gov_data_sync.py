"""政府開放資料同步 API。

權限：所有 endpoint 要求 Permission.SALARY_WRITE（與 api/insurance.py 級距 bulk upsert 一致）。
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from models.database import (
    GovDataSnapshot,
    InsuranceBracketsStaging,
    MinimumWageStaging,
    session_scope,
)
from services.gov_data import promoter
from services.gov_data.schemas import SOURCE_KEYS
from services import gov_data_scheduler
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/gov-data", tags=["gov-data"])

# 一次建立 dependency 實例，方便測試 override
_DEP_SALARY_WRITE = require_staff_permission(Permission.SALARY_WRITE)


class PromoteRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=500)


@router.get("/staging")
async def list_staging(current_user: dict = Depends(_DEP_SALARY_WRITE)) -> dict:
    """列出 6 類 source 狀態 + brackets / minimum_wage 的最新 pending。"""
    with session_scope() as s:
        sources = []
        for src in SOURCE_KEYS:
            latest = (
                s.query(GovDataSnapshot)
                .filter(GovDataSnapshot.source == src)
                .order_by(GovDataSnapshot.fetched_at.desc())
                .first()
            )
            sources.append(
                {
                    "source": src,
                    "last_fetched_at": (
                        latest.fetched_at.isoformat() if latest else None
                    ),
                    "http_status": latest.http_status if latest else None,
                    "error": latest.error if latest else None,
                }
            )

        brackets_pending = (
            s.query(InsuranceBracketsStaging)
            .filter(InsuranceBracketsStaging.status == "pending")
            .order_by(InsuranceBracketsStaging.composed_at.desc())
            .all()
        )
        mw_pending = (
            s.query(MinimumWageStaging)
            .filter(MinimumWageStaging.status == "pending")
            .order_by(MinimumWageStaging.composed_at.desc())
            .all()
        )

        return {
            "sources": sources,
            "brackets_pending": [
                {
                    "id": b.id,
                    "effective_year": b.effective_year,
                    "composed_at": b.composed_at.isoformat(),
                    "diff_summary": b.diff_summary,
                }
                for b in brackets_pending
            ],
            "minimum_wage_pending": [
                {
                    "id": m.id,
                    "effective_date": m.effective_date.isoformat(),
                    "monthly": m.monthly,
                    "hourly": m.hourly,
                    "composed_at": m.composed_at.isoformat(),
                }
                for m in mw_pending
            ],
        }


@router.get("/staging/brackets/{staging_id}/diff")
async def get_brackets_diff(
    staging_id: int, current_user: dict = Depends(_DEP_SALARY_WRITE)
) -> dict:
    with session_scope() as s:
        st = s.get(InsuranceBracketsStaging, staging_id)
        if st is None:
            raise HTTPException(404, "staging 不存在")
        return {
            "id": st.id,
            "effective_year": st.effective_year,
            "status": st.status,
            "diff_summary": st.diff_summary,
            "brackets": st.brackets,
            "rates": st.rates,
        }


def _username(current_user: dict) -> str:
    return current_user.get("username") or current_user.get("name") or "unknown"


@router.post("/staging/brackets/{staging_id}/promote")
async def promote_brackets(
    staging_id: int,
    payload: PromoteRequest,
    current_user: dict = Depends(_DEP_SALARY_WRITE),
) -> dict:
    try:
        promoter.promote_brackets(
            staging_id=staging_id,
            decided_by=_username(current_user),
            reason=payload.reason,
        )
    except promoter.PromoteError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.message})
    return {"status": "promoted", "staging_id": staging_id}


@router.post("/staging/brackets/{staging_id}/dismiss")
async def dismiss_brackets(
    staging_id: int,
    payload: PromoteRequest,
    current_user: dict = Depends(_DEP_SALARY_WRITE),
) -> dict:
    try:
        promoter.dismiss_brackets(
            staging_id=staging_id,
            decided_by=_username(current_user),
            reason=payload.reason,
        )
    except promoter.PromoteError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.message})
    return {"status": "dismissed", "staging_id": staging_id}


@router.post("/staging/minimum-wage/{staging_id}/promote")
async def promote_minimum_wage(
    staging_id: int,
    payload: PromoteRequest,
    current_user: dict = Depends(_DEP_SALARY_WRITE),
) -> dict:
    try:
        promoter.promote_minimum_wage(
            staging_id=staging_id,
            decided_by=_username(current_user),
            reason=payload.reason,
        )
    except promoter.PromoteError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.message})
    return {"status": "promoted", "staging_id": staging_id}


@router.post("/staging/minimum-wage/{staging_id}/dismiss")
async def dismiss_minimum_wage(
    staging_id: int,
    payload: PromoteRequest,
    current_user: dict = Depends(_DEP_SALARY_WRITE),
) -> dict:
    try:
        promoter.dismiss_minimum_wage(
            staging_id=staging_id,
            decided_by=_username(current_user),
            reason=payload.reason,
        )
    except promoter.PromoteError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.message})
    return {"status": "dismissed", "staging_id": staging_id}


@router.post("/sync-now")
async def sync_now_endpoint(current_user: dict = Depends(_DEP_SALARY_WRITE)) -> dict:
    return gov_data_scheduler.sync_now()


@router.get("/snapshots")
async def list_snapshots(
    source: Optional[str] = None,
    limit: int = 50,
    current_user: dict = Depends(_DEP_SALARY_WRITE),
) -> List[dict]:
    with session_scope() as s:
        q = s.query(GovDataSnapshot)
        if source:
            q = q.filter(GovDataSnapshot.source == source)
        q = q.order_by(GovDataSnapshot.fetched_at.desc()).limit(min(limit, 200))
        return [
            {
                "id": r.id,
                "source": r.source,
                "fetched_at": r.fetched_at.isoformat(),
                "http_status": r.http_status,
                "payload_hash": r.payload_hash,
                "error": r.error,
                "url": r.source_url,
            }
            for r in q.all()
        ]
