"""訪視記錄 CRUD、匯入、月份格式正規化。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func

from models.base import session_scope
from models.recruitment import (
    RecruitmentMonth,
    RecruitmentPeriod,
    RecruitmentVisit,
)
from utils.auth import require_staff_permission
from utils.permissions import Permission

from api.recruitment.shared import (
    DATASET_SCOPE_ALL,
    ImportRecord,
    RecruitmentVisitCreate,
    RecruitmentVisitUpdate,
    _build_scoped_query,
    _build_source_filter_condition,
    _expand_roc_month_range,
    _extract_expected_label_from_text,
    _extract_roc_month_from_visit_date,
    _normalize_roc_month,
    _parse_period_range,
    _parse_roc_date,
    _safe_normalize_roc_month,
    _to_dict,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recruitment", tags=["recruitment-records"])


@router.get("/records")
def list_recruitment_records(
    month: Optional[str] = Query(None),
    grade: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    referrer: Optional[str] = Query(None),
    has_deposit: Optional[bool] = Query(None),
    no_deposit_reason: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    dataset_scope: str = Query(DATASET_SCOPE_ALL, pattern="^(all)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    with session_scope() as session:
        q = _build_scoped_query(session, dataset_scope)
        if month:
            q = q.filter(RecruitmentVisit.month == month)
        if grade:
            q = q.filter(RecruitmentVisit.grade == grade)
        if source:
            q = q.filter(_build_source_filter_condition(source))
        if referrer:
            q = q.filter(RecruitmentVisit.referrer == referrer)
        if has_deposit is not None:
            q = q.filter(RecruitmentVisit.has_deposit == has_deposit)
        if no_deposit_reason:
            q = q.filter(RecruitmentVisit.no_deposit_reason == no_deposit_reason)
        if keyword:
            kw = f"%{keyword}%"
            q = q.filter(
                RecruitmentVisit.child_name.ilike(kw)
                | RecruitmentVisit.address.ilike(kw)
                | RecruitmentVisit.notes.ilike(kw)
                | RecruitmentVisit.parent_response.ilike(kw)
            )
        total = q.count()
        records = (
            q.order_by(
                RecruitmentVisit.visit_date.desc().nulls_last(),
                RecruitmentVisit.month.desc(),
                RecruitmentVisit.seq_no,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "records": [_to_dict(r) for r in records],
        }


def normalize_existing_months() -> int:
    """將 DB 中月份格式從 115.3 統一正規化為 115.03（幂等，啟動時呼叫）"""
    with session_scope() as session:
        updated = 0
        for r in (
            session.query(RecruitmentVisit)
            .filter(RecruitmentVisit.month.isnot(None), RecruitmentVisit.month != "")
            .all()
        ):
            normalized = _extract_roc_month_from_visit_date(
                r.visit_date
            ) or _safe_normalize_roc_month(r.month)
            if normalized and normalized != r.month:
                r.month = normalized
                updated += 1
        for r in (
            session.query(RecruitmentMonth)
            .filter(RecruitmentMonth.month.isnot(None), RecruitmentMonth.month != "")
            .all()
        ):
            normalized = _safe_normalize_roc_month(r.month)
            if normalized and normalized != r.month:
                r.month = normalized
                updated += 1
    if updated:
        logger.info("normalize_existing_months: 正規化 %d 筆月份格式", updated)
    return updated


def _auto_sync_periods_for_months(session, months: set) -> None:
    """CRUD 後自動重算受影響月份所在期間的統計數字。"""
    dep_case = case((RecruitmentVisit.has_deposit == True, 1), else_=0)
    for p in session.query(RecruitmentPeriod).all():
        period_range = _parse_period_range(p.period_name)
        if not period_range:
            continue
        period_labels = _expand_roc_month_range(*period_range)
        if not (months & period_labels):
            continue
        row = (
            session.query(
                func.count(RecruitmentVisit.id).label("visit_count"),
                func.sum(dep_case).label("deposit_count"),
                func.sum(case((RecruitmentVisit.enrolled == True, 1), else_=0)).label(
                    "enrolled_count"
                ),
                func.sum(
                    case((RecruitmentVisit.transfer_term == True, 1), else_=0)
                ).label("transfer_term_count"),
            )
            .filter(RecruitmentVisit.month.in_(period_labels))
            .one()
        )
        p.visit_count = row.visit_count or 0
        p.deposit_count = row.deposit_count or 0
        p.enrolled_count = row.enrolled_count or 0
        p.transfer_term_count = row.transfer_term_count or 0
        p.effective_deposit_count = max(
            (row.deposit_count or 0) - (row.transfer_term_count or 0), 0
        )
        p.updated_at = datetime.now()
        logger.info("自動同步期間 [%s] 完成", p.period_name)


@router.post("/records", status_code=201)
def create_recruitment_record(
    payload: RecruitmentVisitCreate,
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        record = RecruitmentVisit(**payload.model_dump())
        record.expected_start_label = _extract_expected_label_from_text(
            record.notes, record.parent_response, record.grade
        )
        session.add(record)
        session.flush()
        _auto_sync_periods_for_months(session, {record.month})
        return _to_dict(record)


@router.put("/records/{record_id}")
def update_recruitment_record(
    record_id: int,
    payload: RecruitmentVisitUpdate,
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        record = session.query(RecruitmentVisit).get(record_id)
        if not record:
            raise HTTPException(status_code=404, detail="紀錄不存在")
        old_month = record.month
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(record, field, value)
        record.expected_start_label = _extract_expected_label_from_text(
            record.notes, record.parent_response, record.grade
        )
        record.updated_at = datetime.now()
        session.flush()
        _auto_sync_periods_for_months(session, {old_month, record.month})
        return _to_dict(record)


@router.delete("/records/{record_id}", status_code=204)
def delete_recruitment_record(
    record_id: int,
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        record = session.query(RecruitmentVisit).get(record_id)
        if not record:
            raise HTTPException(status_code=404, detail="紀錄不存在")
        month = record.month
        session.delete(record)
        session.flush()
        _auto_sync_periods_for_months(session, {month})


@router.post("/import", status_code=201)
def import_recruitment_records(
    records: List[ImportRecord],
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        existing = set(
            (
                (r.child_name or "").strip(),
                (r.month or "").strip(),
                (r.seq_no or "").strip(),
                (r.visit_date or "").strip(),
            )
            for r in session.query(
                RecruitmentVisit.child_name,
                RecruitmentVisit.month,
                RecruitmentVisit.seq_no,
                RecruitmentVisit.visit_date,
            ).all()
        )
        inserted = 0
        skipped = 0
        for rec in records:
            name = (rec.幼生姓名 or "").strip()
            raw_month = (rec.月份 or "").strip()
            seq_no = (rec.序號 or "").strip()
            visit_date = (rec.日期 or "").strip()
            try:
                month = _normalize_roc_month(raw_month) if raw_month else ""
            except ValueError:
                skipped += 1
                continue
            month = _extract_roc_month_from_visit_date(visit_date) or month
            if not name or not month:
                skipped += 1
                continue
            dedup_key = (name, month, seq_no, visit_date)
            if dedup_key in existing:
                skipped += 1
                continue
            visit = RecruitmentVisit(
                month=month,
                seq_no=rec.序號,
                visit_date=rec.日期,
                child_name=name,
                birthday=_parse_roc_date(rec.生日),
                grade=rec.適讀班級,
                phone=rec.電話,
                address=rec.地址,
                district=rec.行政區,
                source=rec.幼生來源,
                referrer=rec.介紹者,
                deposit_collector=rec.收預繳人員,
                has_deposit=(rec.是否預繳 == "是"),
                notes=rec.備註,
                parent_response=rec.電訪後家長回應,
            )
            visit.expected_start_label = _extract_expected_label_from_text(
                visit.notes, visit.parent_response, visit.grade
            )
            session.add(visit)
            existing.add(dedup_key)
            inserted += 1
        logger.info(f"招生資料匯入：插入 {inserted} 筆，跳過 {skipped} 筆")
        return {"inserted": inserted, "skipped": skipped}
