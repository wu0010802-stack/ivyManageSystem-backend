"""園所設定、鄰近幼兒園查詢、市場情報。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from models.base import session_scope
from services import recruitment_market_intelligence as market_service
from utils.auth import require_staff_permission
from utils.permissions import Permission

from api.recruitment.shared import (
    CampusSettingPayload,
    DATASET_SCOPE_ALL,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/recruitment", tags=["recruitment-market"])


@router.get("/campus-setting")
def get_recruitment_campus_setting(
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    with session_scope() as session:
        setting = market_service.get_or_create_campus_setting(session)
        return market_service.serialize_campus_setting(setting)


@router.put("/campus-setting")
def update_recruitment_campus_setting(
    payload: CampusSettingPayload,
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        setting = market_service.upsert_campus_setting(session, payload.model_dump())
        return market_service.serialize_campus_setting(setting)


@router.get("/nearby-kindergartens")
def get_nearby_kindergartens(
    south: float = Query(None, ge=-90, le=90, description="視野南界緯度"),
    west: float = Query(None, ge=-180, le=180, description="視野西界經度"),
    north: float = Query(None, ge=-90, le=90, description="視野北界緯度"),
    east: float = Query(None, ge=-180, le=180, description="視野東界經度"),
    zoom: int = Query(None, ge=1, le=22, description="地圖縮放等級"),
    radius_km: float = Query(
        None, ge=0.5, le=50.0, description="以本園為圓心的查詢半徑（向下相容）"
    ),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    bounds = None
    if all(v is not None for v in (south, west, north, east)):
        bounds = {
            "south": south,
            "west": west,
            "north": north,
            "east": east,
            "zoom": zoom,
        }
    with session_scope() as session:
        return market_service.search_nearby_kindergartens(
            session,
            radius_km=radius_km or 10.0,
            bounds=bounds,
        )


@router.post("/market-intelligence/sync")
def sync_recruitment_market_intelligence(
    hotspot_limit: int = Query(200, ge=50, le=500),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_WRITE)),
):
    with session_scope() as session:
        result = market_service.sync_market_intelligence(
            session, hotspot_limit=hotspot_limit
        )
        snapshot = market_service.build_market_intelligence_snapshot(session)
        return {
            **result,
            "snapshot": snapshot,
        }


@router.get("/market-intelligence")
def get_recruitment_market_intelligence(
    dataset_scope: str = Query(DATASET_SCOPE_ALL, pattern="^(all)$"),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    with session_scope() as session:
        return market_service.build_market_intelligence_snapshot(
            session, dataset_scope=dataset_scope
        )
