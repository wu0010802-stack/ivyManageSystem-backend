"""地址熱點聚合與 geocode 同步。"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func

from models.base import session_scope
from models.recruitment import RecruitmentGeocodeCache, RecruitmentIvykidsRecord, RecruitmentVisit
from services import recruitment_market_intelligence as market_service
from services.geocoding_service import truncate_address_to_lane
from utils.auth import require_staff_permission
from utils.permissions import Permission

from collections import defaultdict

from api.recruitment.shared import (
    DATASET_SCOPE_ALL,
    _dataset_scope_filters,
    _extract_district_from_address,
    _is_google_stale_cache,
    _load_hotspot_cache_rows,
    _needs_incremental_sync,
    _normalize_hotspot_sync_mode,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recruitment", tags=["recruitment-hotspots"])


def _build_buckets_response(all_hotspots: list[dict], cache_rows: dict) -> dict:
    """100m grid bucket + K-anonymity suppression.

    all_hotspots: list of {address, district, visit, deposit} -- address is already truncated
    cache_rows: dict address -> RecruitmentGeocodeCache

    Returns: {"buckets": [...], "district_residual_visits": {...}}
    """
    from config import get_settings

    k_threshold = get_settings().recruitment.k_anonymity_threshold
    K = max(2, min(10, k_threshold))

    grid_acc: dict = defaultdict(
        lambda: {
            "visit": 0,
            "deposit": 0,
            "lat_sum": 0.0,
            "lng_sum": 0.0,
            "n": 0,
            "district": "",
        }
    )
    for hotspot in all_hotspots:
        cached = cache_rows.get(hotspot["address"])
        if not cached or cached.lat is None or cached.lng is None:
            continue
        if cached.status != "resolved":
            continue
        # Grid key: round to 3 decimals (~100m)
        grid_lat = round(cached.lat * 1000) / 1000
        grid_lng = round(cached.lng * 1000) / 1000
        district = hotspot.get("district") or cached.district or "未填寫"
        key = (grid_lat, grid_lng, district)
        bucket = grid_acc[key]
        bucket["visit"] += hotspot["visit"]
        bucket["deposit"] += hotspot["deposit"]
        # Density-weighted center: each hotspot contributes once
        bucket["lat_sum"] += cached.lat
        bucket["lng_sum"] += cached.lng
        bucket["n"] += 1
        bucket["district"] = district

    buckets = []
    residual: dict[str, int] = {}
    for (_grid_lat, _grid_lng, district), b in grid_acc.items():
        if b["visit"] >= K:
            buckets.append(
                {
                    "center_lat": b["lat_sum"] / b["n"],
                    "center_lng": b["lng_sum"] / b["n"],
                    "district": b["district"],
                    "visit_count": b["visit"],
                    "deposit_count": b["deposit"],
                }
            )
        else:
            residual[district] = residual.get(district, 0) + b["visit"]

    return {"buckets": buckets, "district_residual_visits": residual}


def _query_address_hotspots(
    session,
    limit: Optional[int] = None,
    dataset_scope: Optional[str] = None,
) -> tuple[list[dict], int, int]:
    dep_case = case((RecruitmentVisit.has_deposit == True, 1), else_=0)
    normalized_address = func.trim(RecruitmentVisit.address)
    rows_query = session.query(
        normalized_address.label("address"),
        RecruitmentVisit.district.label("district"),
        func.count(RecruitmentVisit.id).label("visit"),
        func.sum(dep_case).label("deposit"),
    )
    scope_filters = _dataset_scope_filters(dataset_scope)
    if scope_filters:
        rows_query = rows_query.filter(*scope_filters)
    rows = (
        rows_query.filter(
            RecruitmentVisit.address.isnot(None),
            func.length(normalized_address) > 0,
            RecruitmentVisit.geocoding_consent_at.isnot(None),
        )
        .group_by(normalized_address, RecruitmentVisit.district)
        .all()
    )

    # 業主決議 §4.7：ivykids 同 hotspot pipeline；預設 consent NULL → 預設不上 heatmap
    ivy_dep_case = case((RecruitmentIvykidsRecord.has_deposit == True, 1), else_=0)
    ivy_addr = func.trim(RecruitmentIvykidsRecord.address)
    ivy_rows = (
        session.query(
            ivy_addr.label("address"),
            RecruitmentIvykidsRecord.district.label("district"),
            func.count(RecruitmentIvykidsRecord.id).label("visit"),
            func.sum(ivy_dep_case).label("deposit"),
        )
        .filter(
            RecruitmentIvykidsRecord.address.isnot(None),
            func.length(ivy_addr) > 0,
            RecruitmentIvykidsRecord.geocoding_consent_at.isnot(None),
        )
        .group_by(ivy_addr, RecruitmentIvykidsRecord.district)
        .all()
    )

    merged: dict[str, dict] = {}
    records_with_address = 0
    # 兩來源同 loop：visit + ivykids 用同 truncated address key 累加
    all_rows = list(rows) + list(ivy_rows)
    for row in all_rows:
        raw_address = (row.address or "").strip()
        if not raw_address:
            continue
        # PII 降精度：以巷級 truncated address 為 hotspot key
        # (dialect-agnostic: SQL 只 trim，Python 端 truncate 後再 group)
        address = truncate_address_to_lane(raw_address)
        if not address:
            continue

        district = (
            (row.district or "").strip()
            or _extract_district_from_address(address)
            or "未填寫"
        )
        visit = row.visit or 0
        deposit = row.deposit or 0
        records_with_address += visit

        hotspot = merged.setdefault(
            address,
            {
                "address": address,
                "district": district,
                "visit": 0,
                "deposit": 0,
            },
        )
        hotspot["visit"] += visit
        hotspot["deposit"] += deposit
        if hotspot["district"] == "未填寫" and district != "未填寫":
            hotspot["district"] = district

    hotspots = sorted(
        merged.values(),
        key=lambda item: (-item["visit"], item["address"]),
    )
    if limit is not None:
        hotspots = hotspots[:limit]
    return hotspots, records_with_address, len(merged)


def _build_address_hotspots_response(
    session, limit: int, dataset_scope: Optional[str] = None
) -> dict:
    all_hotspots, records_with_address, total_hotspots = _query_address_hotspots(
        session,
        dataset_scope=dataset_scope,
    )
    hotspots = all_hotspots[:limit]
    cache_rows = _load_hotspot_cache_rows(
        session,
        [hotspot["address"] for hotspot in all_hotspots],
    )

    geocoded_hotspots = 0
    failed_hotspots = 0
    stale_hotspots = 0
    for hotspot in all_hotspots:
        cached = cache_rows.get(hotspot["address"])
        status = cached.status if cached else "pending"
        lat = cached.lat if cached and cached.status == "resolved" else None
        lng = cached.lng if cached and cached.status == "resolved" else None
        if lat is not None and lng is not None:
            geocoded_hotspots += 1
        elif status == "failed":
            failed_hotspots += 1
        if _is_google_stale_cache(cached):
            stale_hotspots += 1

    enriched_hotspots = []
    for hotspot in hotspots:
        cached = cache_rows.get(hotspot["address"])
        status = cached.status if cached else "pending"
        lat = cached.lat if cached and cached.status == "resolved" else None
        lng = cached.lng if cached and cached.status == "resolved" else None
        enriched_hotspots.append(
            {
                **hotspot,
                "lat": lat,
                "lng": lng,
                "geocode_status": status,
                "provider": cached.provider if cached else None,
                "formatted_address": cached.formatted_address if cached else None,
                "matched_address": cached.matched_address if cached else None,
                "google_place_id": cached.google_place_id if cached else None,
                "town_code": cached.town_code if cached else None,
                "town_name": cached.town_name if cached else None,
                "county_name": cached.county_name if cached else None,
                "land_use_label": cached.land_use_label if cached else None,
                "travel_minutes": cached.travel_minutes if cached else None,
                "travel_distance_km": cached.travel_distance_km if cached else None,
                "data_quality": cached.data_quality if cached else "partial",
            }
        )

    pending_hotspots = max(total_hotspots - geocoded_hotspots - failed_hotspots, 0)
    provider_name = market_service.current_market_provider()
    bucket_payload = _build_buckets_response(all_hotspots, cache_rows)
    return {
        "records_with_address": records_with_address,
        "total_hotspots": total_hotspots,
        "geocoded_hotspots": geocoded_hotspots,
        "pending_hotspots": pending_hotspots,
        "remaining_hotspots": pending_hotspots,
        "failed_hotspots": failed_hotspots,
        "stale_hotspots": stale_hotspots,
        "provider_available": provider_name is not None,
        "provider_name": provider_name,
        "hotspots": enriched_hotspots,
        **bucket_payload,
    }


@router.get("/address-hotspots")
def get_recruitment_address_hotspots(
    limit: int = Query(200, ge=1, le=500),
    dataset_scope: str = Query(DATASET_SCOPE_ALL, pattern="^(all)$"),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    """依完整地址聚合的熱點資料，供區域分析簡易地圖使用。"""
    with session_scope() as session:
        return _build_address_hotspots_response(
            session, limit, dataset_scope=dataset_scope
        )


@router.post("/address-hotspots/sync")
def sync_recruitment_address_hotspots(
    batch_size: int = Query(10, ge=1, le=20),
    limit: int = Query(200, ge=1, le=500),
    sync_mode: str = "incremental",
    dataset_scope: str = Query(DATASET_SCOPE_ALL, pattern="^(all)$"),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    """同步一小批地址座標到快取，避免每次前端渲染時重複 geocode。"""
    if not market_service.market_provider_available():
        raise HTTPException(
            status_code=400, detail="尚未設定 Google / TGOS / geocoding provider"
        )
    normalized_sync_mode = _normalize_hotspot_sync_mode(sync_mode)

    with session_scope() as session:
        hotspots, _records_with_address, _total_hotspots = _query_address_hotspots(
            session,
            dataset_scope=dataset_scope,
        )
        addresses = [hotspot["address"] for hotspot in hotspots]
        cached_rows = _load_hotspot_cache_rows(session, addresses)

        campus = market_service.serialize_campus_setting(
            market_service.get_or_create_campus_setting(session)
        )
        eligible_targets: list[tuple[dict, Optional[RecruitmentGeocodeCache]]] = []
        skipped = 0
        for hotspot in hotspots:
            cached = cached_rows.get(hotspot["address"])
            should_sync = (
                _needs_incremental_sync(cached)
                if normalized_sync_mode == "incremental"
                else _is_google_stale_cache(cached)
            )
            if should_sync:
                eligible_targets.append((hotspot, cached))
            else:
                skipped += 1

        sync_targets = eligible_targets[:batch_size]
        attempted = len(sync_targets)
        synced = 0
        failed = 0
        for hotspot, cached in sync_targets:
            result = market_service.resolve_address_metadata(
                hotspot["address"], campus=campus
            )
            if not cached:
                cached = RecruitmentGeocodeCache(address=hotspot["address"])
                session.add(cached)
                cached_rows[cached.address] = cached

            market_service._apply_metadata_to_geocode_cache(
                cached,
                result or {},
                district=hotspot["district"],
            )
            if cached.status == "resolved":
                synced += 1
            else:
                failed += 1

        session.flush()
        response = _build_address_hotspots_response(
            session, limit, dataset_scope=dataset_scope
        )
        response["sync_mode"] = normalized_sync_mode
        response["attempted"] = attempted
        response["synced"] = synced
        response["failed"] = failed
        response["skipped"] = skipped
        return response
