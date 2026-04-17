"""競爭幼兒園（教育部資料 + kiang + Google）相關 API。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_

from models.base import session_scope
from services import recruitment_market_intelligence as market_service
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recruitment", tags=["recruitment-competitors"])


@router.get("/competitor-schools/geocode-pending")
def get_geocode_pending_count(
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    """查詢尚無座標的 competitor_school 數量（輕量查詢，不消耗 Google API）。"""
    with session_scope() as session:
        from models.competitor_school import CompetitorSchool

        count = (
            session.query(CompetitorSchool)
            .filter(
                CompetitorSchool.latitude.is_(None),
                CompetitorSchool.city.like("%高雄%"),
                CompetitorSchool.is_active == True,  # noqa: E712
            )
            .count()
        )
        return {"pending": count}


@router.post("/competitor-schools/sync-kiang")
def sync_kiang_data(
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    """從 kiang.github.io 補充月費、面積、接駁等欄位（不消耗 Google API）。"""
    import requests
    from services.moe_kindergarten_scraper import _sync_kiang_supplementary

    with session_scope() as session:
        http_sess = requests.Session()
        updated = _sync_kiang_supplementary(http_sess, session)
        return {"updated": updated}


@router.get("/campus-competition")
def get_campus_competition(
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    """各常春藤校區的周遭競爭分析（3km / 6km）。"""
    from sqlalchemy import text
    from models.recruitment import CompetitorSchool

    with session_scope() as session:
        ivy_schools = (
            session.query(CompetitorSchool)
            .filter(
                CompetitorSchool.is_active == True,  # noqa: E712
                or_(
                    CompetitorSchool.school_name.like("%常春藤%"),
                    CompetitorSchool.school_name.like("%明華幼兒園%"),
                ),
                CompetitorSchool.latitude.isnot(None),
            )
            .order_by(CompetitorSchool.school_name)
            .all()
        )

        result = []
        for ivy in ivy_schools:
            sql = text("""
                SELECT
                    cs.id, cs.school_name, cs.school_type, cs.pre_public_type,
                    cs.approved_capacity, cs.monthly_fee, cs.has_penalty, cs.district,
                    round(
                        (6371 * acos(
                            cos(radians(:lat)) * cos(radians(cs.latitude)) *
                            cos(radians(cs.longitude) - radians(:lng)) +
                            sin(radians(:lat)) * sin(radians(cs.latitude))
                        ))::numeric, 2
                    ) AS distance_km
                FROM competitor_school cs
                WHERE cs.is_active
                  AND cs.latitude IS NOT NULL
                  AND cs.id != :ivy_id
                  AND (6371 * acos(
                        cos(radians(:lat)) * cos(radians(cs.latitude)) *
                        cos(radians(cs.longitude) - radians(:lng)) +
                        sin(radians(:lat)) * sin(radians(cs.latitude))
                      )) <= 6
                ORDER BY distance_km
            """)
            rows = session.execute(
                sql, {"lat": ivy.latitude, "lng": ivy.longitude, "ivy_id": ivy.id}
            ).fetchall()

            def classify(row):
                name = row.school_name or ""
                if "常春藤" in name or "明華幼兒園" in name:
                    return "常春藤"
                if row.pre_public_type:
                    return "準公共"
                return row.school_type or "其他"

            rings = {}
            for label, max_km in [("3km", 3), ("6km", 6)]:
                in_range = [r for r in rows if r.distance_km <= max_km]
                by_type = {}
                for r in in_range:
                    t = classify(r)
                    if t not in by_type:
                        by_type[t] = {
                            "count": 0,
                            "total_capacity": 0,
                            "fees": [],
                            "penalty_count": 0,
                        }
                    by_type[t]["count"] += 1
                    by_type[t]["total_capacity"] += r.approved_capacity or 0
                    if r.monthly_fee:
                        by_type[t]["fees"].append(r.monthly_fee)
                    if r.has_penalty:
                        by_type[t]["penalty_count"] += 1

                type_stats = []
                for t, s in sorted(by_type.items(), key=lambda x: -x[1]["count"]):
                    type_stats.append(
                        {
                            "type": t,
                            "count": s["count"],
                            "avg_capacity": (
                                round(s["total_capacity"] / s["count"])
                                if s["count"]
                                else 0
                            ),
                            "avg_fee": (
                                round(sum(s["fees"]) / len(s["fees"]))
                                if s["fees"]
                                else None
                            ),
                            "penalty_count": s["penalty_count"],
                        }
                    )
                rings[label] = {
                    "total": len(in_range),
                    "total_capacity": sum(r.approved_capacity or 0 for r in in_range),
                    "types": type_stats,
                }

            result.append(
                {
                    "school_name": ivy.school_name,
                    "district": ivy.district,
                    "lat": float(ivy.latitude),
                    "lng": float(ivy.longitude),
                    "approved_capacity": ivy.approved_capacity,
                    "monthly_fee": ivy.monthly_fee,
                    "rings": rings,
                }
            )

        return {"campuses": result}


@router.post("/competitor-schools/geocode")
def geocode_competitor_schools(
    limit: int = Query(100, ge=1, le=500, description="本次最多 geocode 的學校筆數"),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    """批量 geocode 尚無座標的 competitor_school 記錄，結果存回 DB。"""
    with session_scope() as session:
        return market_service.geocode_all_competitor_schools(session, limit=limit)
