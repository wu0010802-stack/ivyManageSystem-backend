"""
api/recruitment_ivykids.py — 義華校官網報名 API endpoints
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_

from models.base import session_scope
from models.recruitment import RecruitmentIvykidsRecord, RecruitmentSyncState
from services import recruitment_ivykids_sync as ivykids_sync_service
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recruitment/ivykids", tags=["recruitment-ivykids"])


class IvykidsBackendSyncPayload(BaseModel):
    max_pages: int = Field(ivykids_sync_service.MAX_SYNC_PAGES, ge=1, le=100)


def _clean_text(value: Optional[str], fallback: str = "未填寫") -> str:
    text = " ".join(str(value or "").strip().split())
    return text or fallback


def _roc_month_sort_key(value: Optional[str]) -> tuple[int, int, str]:
    text = _clean_text(value, "")
    if not text or "." not in text:
        return (999999, 99, text)

    year_text, month_text = text.split(".", 1)
    if not year_text.isdigit() or not month_text.isdigit():
        return (999998, 99, text)
    return (int(year_text), int(month_text), text)


def _to_dict(record: RecruitmentIvykidsRecord) -> dict:
    return {
        "id": record.id,
        "external_id": record.external_id,
        "external_status": record.external_status,
        "external_created_at": record.external_created_at,
        "month": record.month,
        "visit_date": record.visit_date,
        "child_name": record.child_name,
        "birthday": record.birthday.isoformat() if record.birthday else None,
        "grade": record.grade,
        "phone": record.phone,
        "address": record.address,
        "district": record.district,
        "source": record.source,
        "referrer": record.referrer,
        "deposit_collector": record.deposit_collector,
        "notes": record.notes,
        "parent_response": record.parent_response,
        "has_deposit": record.has_deposit,
        "enrolled": record.enrolled,
        "transfer_term": record.transfer_term,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


def _apply_reporting_window(query):
    cutoff = ivykids_sync_service.get_sync_created_at_cutoff()
    if cutoff is None:
        return query
    return query.filter(
        RecruitmentIvykidsRecord.external_created_at >= cutoff.strftime("%Y-%m-%d %H:%M:%S")
    )


@router.get("/status")
def get_recruitment_ivykids_backend_status(
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    with session_scope() as session:
        return ivykids_sync_service.get_backend_sync_status(session)


@router.post("/sync")
def sync_recruitment_ivykids_backend(
    payload: IvykidsBackendSyncPayload,
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        result = ivykids_sync_service.sync_backend_records(
            session,
            max_pages=payload.max_pages,
            trigger="manual",
        )
        if result.get("provider_available"):
            logger.info(
                "義華校官網同步：抓取 %s 筆，新增 %s 筆，更新 %s 筆，略過 %s 筆",
                result.get("total_fetched", 0),
                result.get("inserted", 0),
                result.get("updated", 0),
                result.get("skipped", 0),
            )
        else:
            logger.warning("義華校官網同步未啟用：%s", result.get("message"))
        return result


@router.delete("/records", status_code=200)
def delete_recruitment_ivykids_backend_records(
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        deleted_records = session.query(RecruitmentIvykidsRecord).delete(synchronize_session=False)
        deleted_states = (
            session.query(RecruitmentSyncState)
            .filter(RecruitmentSyncState.provider_name == ivykids_sync_service.IVYKIDS_BACKEND_SOURCE)
            .delete(synchronize_session=False)
        )
        logger.warning(
            "義華官網報名資料已清除：刪除 %d 筆官網記錄，重置 %d 筆同步狀態",
            deleted_records,
            deleted_states,
        )
        return {"deleted": deleted_records, "reset_states": deleted_states}


@router.get("/stats")
def get_recruitment_ivykids_stats(
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    dep_case = case((RecruitmentIvykidsRecord.has_deposit == True, 1), else_=0)
    enrolled_case = case((RecruitmentIvykidsRecord.enrolled == True, 1), else_=0)

    with session_scope() as session:
        base_query = _apply_reporting_window(session.query(RecruitmentIvykidsRecord))
        total_row = base_query.with_entities(
            func.count(RecruitmentIvykidsRecord.id),
            func.sum(dep_case),
            func.sum(enrolled_case),
        ).one()
        by_source_rows = (
            _apply_reporting_window(
                session.query(
                    RecruitmentIvykidsRecord.source,
                    func.count(RecruitmentIvykidsRecord.id).label("visit"),
                    func.sum(dep_case).label("deposit"),
                )
            )
            .group_by(RecruitmentIvykidsRecord.source)
            .order_by(func.count(RecruitmentIvykidsRecord.id).desc(), RecruitmentIvykidsRecord.source)
            .all()
        )
        by_month_rows = (
            _apply_reporting_window(
                session.query(
                    RecruitmentIvykidsRecord.month,
                    func.count(RecruitmentIvykidsRecord.id).label("visit"),
                    func.sum(dep_case).label("deposit"),
                    func.sum(enrolled_case).label("enrolled"),
                )
            )
            .group_by(RecruitmentIvykidsRecord.month)
            .all()
        )

    return {
        "total_visit": total_row[0] or 0,
        "total_deposit": total_row[1] or 0,
        "total_enrolled": total_row[2] or 0,
        "by_source": [
            {
                "source": _clean_text(row.source),
                "visit": row.visit or 0,
                "deposit": row.deposit or 0,
            }
            for row in by_source_rows
        ],
        "by_month": sorted(
            [
                {
                    "month": row.month,
                    "visit": row.visit or 0,
                    "deposit": row.deposit or 0,
                    "enrolled": row.enrolled or 0,
                }
                for row in by_month_rows
            ],
            key=lambda item: _roc_month_sort_key(item["month"]),
        ),
    }


@router.get("/records")
def list_recruitment_ivykids_records(
    month: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    with session_scope() as session:
        query = _apply_reporting_window(session.query(RecruitmentIvykidsRecord))
        if month:
            query = query.filter(RecruitmentIvykidsRecord.month == month)
        if source:
            if source == "未填寫":
                query = query.filter(
                    or_(
                        RecruitmentIvykidsRecord.source.is_(None),
                        RecruitmentIvykidsRecord.source == "",
                    )
                )
            else:
                query = query.filter(RecruitmentIvykidsRecord.source == source)

        total = query.count()
        records = (
            query.order_by(
                RecruitmentIvykidsRecord.month.desc(),
                RecruitmentIvykidsRecord.external_created_at.desc(),
                RecruitmentIvykidsRecord.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "records": [_to_dict(item) for item in records],
        }
