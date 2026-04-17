"""近五年期間、選項列表、手動月份登記。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

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
    NO_DEPOSIT_REASONS,
    MonthCreate,
    PeriodCreate,
    PeriodUpdate,
    _build_period_month_labels,
    _dataset_scope_filters,
    _expand_roc_month_range,
    _normalize_dataset_scope,
    _normalize_source_label,
    _parse_period_range,
    _period_to_dict,
    _roc_month_sort_key,
    _safe_normalize_roc_month,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recruitment", tags=["recruitment-periods"])


@router.get("/periods/summary")
def get_periods_summary(
    dataset_scope: str = Query(DATASET_SCOPE_ALL, pattern="^(all)$"),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    """近五年整體量體 KPI + 班別轉換分析"""
    with session_scope() as session:
        periods = (
            session.query(RecruitmentPeriod)
            .order_by(RecruitmentPeriod.sort_order)
            .all()
        )
        if not periods:
            return {
                "total_visit": 0,
                "total_deposit": 0,
                "total_enrolled": 0,
                "total_effective": 0,
                "total_transfer_term": 0,
                "total_not_enrolled_deposit": 0,
                "total_enrolled_after_school": 0,
                "total_net_enrolled": 0,
                "visit_to_deposit_rate": 0,
                "visit_to_enrolled_rate": 0,
                "deposit_to_enrolled_rate": 0,
                "effective_to_enrolled_rate": 0,
                "period_count": 0,
                "best_visit_to_enrolled": None,
                "worst_visit_to_enrolled": None,
                "best_deposit_to_enrolled": None,
                "worst_deposit_to_enrolled": None,
                "trend": [],
                "by_grade": [],
            }

        def _pct(num, den):
            return round(num / den * 100, 1) if den else 0

        normalized_scope = _normalize_dataset_scope(dataset_scope)
        dep_case = case((RecruitmentVisit.has_deposit == True, 1), else_=0)
        enrolled_case = case((RecruitmentVisit.enrolled == True, 1), else_=0)
        transfer_case = case((RecruitmentVisit.transfer_term == True, 1), else_=0)
        base_filters = _dataset_scope_filters(normalized_scope)

        if normalized_scope == DATASET_SCOPE_ALL:
            tv = sum(p.visit_count or 0 for p in periods)
            td = sum(p.deposit_count or 0 for p in periods)
            te = sum(p.enrolled_count or 0 for p in periods)
            teff = sum(p.effective_deposit_count or 0 for p in periods)
            ttr = sum(p.transfer_term_count or 0 for p in periods)

            trend = [
                {
                    "period_name": p.period_name,
                    "visit_count": p.visit_count or 0,
                    "deposit_count": p.deposit_count or 0,
                    "enrolled_count": p.enrolled_count or 0,
                    "not_enrolled_deposit": p.not_enrolled_deposit or 0,
                    "enrolled_after_school": p.enrolled_after_school or 0,
                    "net_enrolled_count": (p.enrolled_count or 0)
                    - (p.enrolled_after_school or 0),
                    "visit_to_deposit_rate": _pct(
                        p.deposit_count or 0, p.visit_count or 0
                    ),
                    "visit_to_enrolled_rate": _pct(
                        p.enrolled_count or 0, p.visit_count or 0
                    ),
                    "deposit_to_enrolled_rate": _pct(
                        p.enrolled_count or 0, p.deposit_count or 0
                    ),
                    "effective_to_enrolled_rate": _pct(
                        p.enrolled_count or 0, p.effective_deposit_count or 0
                    ),
                }
                for p in periods
            ]
        else:
            trend = []
            tv = td = te = teff = ttr = 0
            for period in periods:
                period_range = _parse_period_range(period.period_name)
                period_month_labels = (
                    _expand_roc_month_range(*period_range) if period_range else set()
                )
                row = None
                if period_month_labels:
                    query = session.query(
                        func.count(RecruitmentVisit.id).label("visit_count"),
                        func.sum(dep_case).label("deposit_count"),
                        func.sum(enrolled_case).label("enrolled_count"),
                        func.sum(transfer_case).label("transfer_term_count"),
                    )
                    if base_filters:
                        query = query.filter(*base_filters)
                    row = query.filter(
                        RecruitmentVisit.month.in_(period_month_labels)
                    ).one()

                visit_count = row.visit_count or 0 if row else 0
                deposit_count = row.deposit_count or 0 if row else 0
                enrolled_count = row.enrolled_count or 0 if row else 0
                transfer_term_count = row.transfer_term_count or 0 if row else 0
                effective_deposit_count = max(deposit_count - transfer_term_count, 0)

                tv += visit_count
                td += deposit_count
                te += enrolled_count
                teff += effective_deposit_count
                ttr += transfer_term_count
                trend.append(
                    {
                        "period_name": period.period_name,
                        "visit_count": visit_count,
                        "deposit_count": deposit_count,
                        "enrolled_count": enrolled_count,
                        "not_enrolled_deposit": 0,
                        "enrolled_after_school": 0,
                        "net_enrolled_count": enrolled_count,
                        "visit_to_deposit_rate": _pct(deposit_count, visit_count),
                        "visit_to_enrolled_rate": _pct(enrolled_count, visit_count),
                        "deposit_to_enrolled_rate": _pct(enrolled_count, deposit_count),
                        "effective_to_enrolled_rate": _pct(
                            enrolled_count, effective_deposit_count
                        ),
                    }
                )

        active = [d for d in trend if d["visit_count"] > 0]
        best_v2e = (
            max(active, key=lambda x: x["visit_to_enrolled_rate"]) if active else None
        )
        worst_v2e = (
            min(active, key=lambda x: x["visit_to_enrolled_rate"]) if active else None
        )
        best_d2e = (
            max(active, key=lambda x: x["deposit_to_enrolled_rate"]) if active else None
        )
        worst_d2e = (
            min(active, key=lambda x: x["deposit_to_enrolled_rate"]) if active else None
        )

        period_month_labels = _build_period_month_labels(periods)
        grade_rows = []
        if period_month_labels:
            grade_query = session.query(
                func.coalesce(RecruitmentVisit.grade, "未填寫").label("grade"),
                func.count(RecruitmentVisit.id).label("visit"),
                func.sum(dep_case).label("deposit"),
                func.sum(case((RecruitmentVisit.enrolled == True, 1), else_=0)).label(
                    "enrolled"
                ),
            )
            if base_filters:
                grade_query = grade_query.filter(*base_filters)
            grade_rows = (
                grade_query.filter(RecruitmentVisit.month.in_(period_month_labels))
                .group_by(RecruitmentVisit.grade)
                .all()
            )

        grade_order = ["幼幼班", "小班", "中班", "大班"]

        def _grade_rates(r) -> dict:
            v, dep, enr = r.visit or 0, r.deposit or 0, r.enrolled or 0
            return {
                "grade": r.grade,
                "visit": v,
                "deposit": dep,
                "enrolled": enr,
                "visit_to_deposit_rate": _pct(dep, v),
                "visit_to_enrolled_rate": _pct(enr, v),
                "deposit_to_enrolled_rate": _pct(enr, dep),
            }

        by_grade_list = sorted(
            [_grade_rates(r) for r in grade_rows],
            key=lambda x: (
                grade_order.index(x["grade"]) if x["grade"] in grade_order else 99
            ),
        )

        return {
            "total_visit": tv,
            "total_deposit": td,
            "total_enrolled": te,
            "total_effective": teff,
            "total_transfer_term": ttr,
            "total_not_enrolled_deposit": (
                sum(p.not_enrolled_deposit or 0 for p in periods)
                if normalized_scope == DATASET_SCOPE_ALL
                else 0
            ),
            "total_enrolled_after_school": (
                sum(p.enrolled_after_school or 0 for p in periods)
                if normalized_scope == DATASET_SCOPE_ALL
                else 0
            ),
            "total_net_enrolled": (
                te - sum(p.enrolled_after_school or 0 for p in periods)
                if normalized_scope == DATASET_SCOPE_ALL
                else te
            ),
            "visit_to_deposit_rate": _pct(td, tv),
            "visit_to_enrolled_rate": _pct(te, tv),
            "deposit_to_enrolled_rate": _pct(te, td),
            "effective_to_enrolled_rate": _pct(te, teff),
            "period_count": len(periods),
            "best_visit_to_enrolled": (
                {
                    "period": best_v2e["period_name"],
                    "rate": best_v2e["visit_to_enrolled_rate"],
                }
                if best_v2e
                else None
            ),
            "worst_visit_to_enrolled": (
                {
                    "period": worst_v2e["period_name"],
                    "rate": worst_v2e["visit_to_enrolled_rate"],
                }
                if worst_v2e
                else None
            ),
            "best_deposit_to_enrolled": (
                {
                    "period": best_d2e["period_name"],
                    "rate": best_d2e["deposit_to_enrolled_rate"],
                }
                if best_d2e
                else None
            ),
            "worst_deposit_to_enrolled": (
                {
                    "period": worst_d2e["period_name"],
                    "rate": worst_d2e["deposit_to_enrolled_rate"],
                }
                if worst_d2e
                else None
            ),
            "trend": trend,
            "by_grade": by_grade_list,
        }


@router.get("/periods")
def list_periods(
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    with session_scope() as session:
        periods = (
            session.query(RecruitmentPeriod)
            .order_by(RecruitmentPeriod.sort_order)
            .all()
        )
        return [_period_to_dict(p) for p in periods]


@router.post("/periods", status_code=201)
def create_period(
    payload: PeriodCreate,
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        existing = (
            session.query(RecruitmentPeriod)
            .filter_by(period_name=payload.period_name)
            .first()
        )
        if existing:
            raise HTTPException(status_code=409, detail="期間名稱已存在")
        p = RecruitmentPeriod(
            **payload.model_dump(), created_at=datetime.now(), updated_at=datetime.now()
        )
        session.add(p)
        session.flush()
        return _period_to_dict(p)


@router.put("/periods/{period_id}")
def update_period(
    period_id: int,
    payload: PeriodUpdate,
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        p = session.query(RecruitmentPeriod).get(period_id)
        if not p:
            raise HTTPException(status_code=404, detail="期間不存在")
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(p, field, value)
        p.updated_at = datetime.now()
        session.flush()
        return _period_to_dict(p)


@router.delete("/periods/{period_id}", status_code=204)
def delete_period(
    period_id: int,
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        p = session.query(RecruitmentPeriod).get(period_id)
        if not p:
            raise HTTPException(status_code=404, detail="期間不存在")
        session.delete(p)


@router.post("/periods/{period_id}/sync")
def sync_period_from_visits(
    period_id: int,
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    """從訪視明細自動計算並更新指定期間的統計數字（依期間名稱解析月份範圍）"""
    with session_scope() as session:
        p = session.query(RecruitmentPeriod).get(period_id)
        if not p:
            raise HTTPException(status_code=404, detail="期間不存在")

        period_range = _parse_period_range(p.period_name)
        if not period_range:
            raise HTTPException(
                status_code=400,
                detail=f"無法從期間名稱解析日期範圍：{p.period_name}，格式應為 111.03.16~111.09.15",
            )

        start_ym, end_ym = period_range
        period_month_labels = _expand_roc_month_range(start_ym, end_ym)
        dep_case = case((RecruitmentVisit.has_deposit == True, 1), else_=0)

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
            .filter(
                RecruitmentVisit.month.in_(period_month_labels),
            )
            .one()
        )

        visit = row.visit_count or 0
        deposit = row.deposit_count or 0
        enrolled = row.enrolled_count or 0
        transfer = row.transfer_term_count or 0
        effective = max(deposit - transfer, 0)

        p.visit_count = visit
        p.deposit_count = deposit
        p.enrolled_count = enrolled
        p.transfer_term_count = transfer
        p.effective_deposit_count = effective
        p.updated_at = datetime.now()

        logger.info(
            f"期間 [{p.period_name}] 已同步：參觀={visit} 預繳={deposit} "
            f"註冊={enrolled} 轉期={transfer} 有效預繳={effective}"
        )
        return _period_to_dict(p)


@router.get("/options")
def get_recruitment_options(
    dataset_scope: str = Query(DATASET_SCOPE_ALL, pattern="^(all)$"),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    """篩選用選項（以 distinct 查詢避免全表掃描回傳到 Python）。"""
    with session_scope() as session:
        scope_filters = _dataset_scope_filters(dataset_scope)

        def _distinct_values(column):
            query = session.query(column)
            if scope_filters:
                query = query.filter(*scope_filters)
            return [
                row[0]
                for row in query.filter(column.isnot(None), column != "")
                .distinct()
                .all()
            ]

        months_set = {
            normalized
            for normalized in (
                _safe_normalize_roc_month(v)
                for v in _distinct_values(RecruitmentVisit.month)
            )
            if normalized
        }
        grades_set = set(_distinct_values(RecruitmentVisit.grade))
        sources_set = {
            _normalize_source_label(value)
            for value in _distinct_values(RecruitmentVisit.source)
        }
        referrers_set = set(_distinct_values(RecruitmentVisit.referrer))

        if _normalize_dataset_scope(dataset_scope) == DATASET_SCOPE_ALL:
            registered = {
                normalized
                for normalized in (
                    _safe_normalize_roc_month(r.month)
                    for r in session.query(RecruitmentMonth.month).all()
                )
                if normalized
            }
            months_set |= registered

        return {
            "months": sorted(months_set, key=_roc_month_sort_key),
            "grades": sorted(grades_set),
            "sources": sorted(sources_set),
            "referrers": sorted(referrers_set),
            "no_deposit_reasons": NO_DEPOSIT_REASONS,
        }


@router.get("/months")
def list_months(
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    """列出所有已手動登記的月份"""
    with session_scope() as session:
        rows = session.query(RecruitmentMonth).all()
        return [
            {"id": r.id, "month": _safe_normalize_roc_month(r.month) or r.month}
            for r in sorted(rows, key=lambda item: _roc_month_sort_key(item.month))
        ]


@router.post("/months", status_code=201)
def create_month(
    payload: MonthCreate,
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    """手動登記一個月份"""
    with session_scope() as session:
        existing = (
            session.query(RecruitmentMonth).filter_by(month=payload.month).first()
        )
        if existing:
            raise HTTPException(status_code=409, detail=f"月份 {payload.month} 已存在")
        rec = RecruitmentMonth(month=payload.month)
        session.add(rec)
        session.flush()
        logger.info(f"手動登記月份：{payload.month}")
        return {"id": rec.id, "month": rec.month}


@router.delete("/months/{month}")
def delete_month(
    month: str,
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    """刪除手動登記月份（不影響該月份的訪視記錄）"""
    with session_scope() as session:
        rec = session.query(RecruitmentMonth).filter_by(month=month).first()
        if not rec:
            raise HTTPException(status_code=404, detail=f"登記月份 {month} 不存在")
        session.delete(rec)
        logger.info(f"刪除登記月份：{month}")
        return {"deleted": month}
