"""政府開放資料同步 API。

權限：所有 endpoint 要求 Permission.SALARY_WRITE（與 api/insurance.py 級距 bulk upsert 一致）。

歷史背景（為何不用 Pydantic Field(min_length=...)）：
    舊版 promote/dismiss endpoint 用 Pydantic `Field(min_length=10)` 驗 reason，
    使用者輸入太短時 FastAPI 回 422，且 detail 是英文 "String should have at
    least 10 characters"。前端遇 422 通常無法翻譯，造成「為什麼一直 422」的
    UX 問題。現改為接 dict 後手動驗證，超短時回 400 + 中文 message，
    讓前端能直接 toast。
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

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

REASON_MIN_LEN = promoter.REASON_MIN_LEN
REASON_MAX_LEN = 500


def _extract_reason(payload: Any) -> str:
    """從 request body 取出 reason 並驗證；失敗一律回 400（避免 Pydantic 422）。"""
    if payload is None:
        raise HTTPException(
            400,
            {
                "code": "REASON_REQUIRED",
                "message": "缺少 request body；請傳 JSON：{\"reason\": \"...\"}",
            },
        )
    if not isinstance(payload, dict):
        raise HTTPException(
            400,
            {
                "code": "REASON_INVALID",
                "message": "request body 必須是 JSON 物件",
            },
        )
    reason = payload.get("reason")
    if not isinstance(reason, str):
        raise HTTPException(
            400,
            {
                "code": "REASON_REQUIRED",
                "message": f"缺少 reason 欄位；必須是 {REASON_MIN_LEN}–{REASON_MAX_LEN} 字的字串",
            },
        )
    reason = reason.strip()
    n = len(reason)
    if n < REASON_MIN_LEN:
        raise HTTPException(
            400,
            {
                "code": "REASON_TOO_SHORT",
                "message": f"reason 必須 ≥ {REASON_MIN_LEN} 字（目前 {n} 字）；外部資料寫入需稽核軌跡。",
            },
        )
    if n > REASON_MAX_LEN:
        raise HTTPException(
            400,
            {
                "code": "REASON_TOO_LONG",
                "message": f"reason 不可超過 {REASON_MAX_LEN} 字（目前 {n} 字）。",
            },
        )
    return reason


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
    payload: dict = Body(default=None),
    current_user: dict = Depends(_DEP_SALARY_WRITE),
) -> dict:
    reason = _extract_reason(payload)
    try:
        promoter.promote_brackets(
            staging_id=staging_id,
            decided_by=_username(current_user),
            reason=reason,
        )
    except promoter.PromoteError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.message})
    return {"status": "promoted", "staging_id": staging_id}


@router.post("/staging/brackets/{staging_id}/dismiss")
async def dismiss_brackets(
    staging_id: int,
    payload: dict = Body(default=None),
    current_user: dict = Depends(_DEP_SALARY_WRITE),
) -> dict:
    reason = _extract_reason(payload)
    try:
        promoter.dismiss_brackets(
            staging_id=staging_id,
            decided_by=_username(current_user),
            reason=reason,
        )
    except promoter.PromoteError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.message})
    return {"status": "dismissed", "staging_id": staging_id}


@router.post("/staging/minimum-wage/{staging_id}/promote")
async def promote_minimum_wage(
    staging_id: int,
    payload: dict = Body(default=None),
    current_user: dict = Depends(_DEP_SALARY_WRITE),
) -> dict:
    reason = _extract_reason(payload)
    try:
        promoter.promote_minimum_wage(
            staging_id=staging_id,
            decided_by=_username(current_user),
            reason=reason,
        )
    except promoter.PromoteError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.message})
    return {"status": "promoted", "staging_id": staging_id}


@router.post("/staging/minimum-wage/{staging_id}/dismiss")
async def dismiss_minimum_wage(
    staging_id: int,
    payload: dict = Body(default=None),
    current_user: dict = Depends(_DEP_SALARY_WRITE),
) -> dict:
    reason = _extract_reason(payload)
    try:
        promoter.dismiss_minimum_wage(
            staging_id=staging_id,
            decided_by=_username(current_user),
            reason=reason,
        )
    except promoter.PromoteError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.message})
    return {"status": "dismissed", "staging_id": staging_id}


@router.post("/sync-now")
async def sync_now_endpoint(current_user: dict = Depends(_DEP_SALARY_WRITE)) -> dict:
    """手動觸發一次完整同步。

    回傳除了原本的 snapshot_ids，多兩個欄位讓前端能直接顯示「為什麼沒抓到」：
    - configured: 每個 source 是否設定 URL（環境變數）
    - warning: 若全部 source 都沒 URL，提示要設環境變數
    這裡刻意不回 400，因為手動觸發的 UX 是「即使部分失敗也想看 partial 結果」。
    """
    from services.gov_data import fetcher as _fetcher

    configured = {k: bool(v) for k, v in _fetcher.SOURCE_URLS.items()}
    try:
        result = gov_data_scheduler.sync_now()
    except Exception as exc:  # noqa: BLE001 — 同步失敗統一吃下去回 502，避免 500
        logger.exception("sync_now 失敗")
        raise HTTPException(
            502,
            {"code": "SYNC_FAILED", "message": f"同步失敗：{exc}"},
        )

    response: dict = {**result, "configured": configured}
    if not any(configured.values()):
        response["warning"] = (
            "未設定任何資料源 URL；請設定環境變數 "
            "GOV_DATA_URL_MOL_LABOR_BRACKETS、GOV_DATA_URL_MOL_LABOR_PREMIUM、"
            "GOV_DATA_URL_MOL_PENSION、GOV_DATA_URL_NHI_BRACKETS、"
            "GOV_DATA_URL_NHI_PREMIUM、GOV_DATA_URL_MOL_MINIMUM_WAGE 後重啟服務。"
        )
    return response


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
