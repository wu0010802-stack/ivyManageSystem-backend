"""Recruitment market router (api/recruitment/market.py) 對應 Out schemas。

Phase 3.5 範圍（本檔）：
- GET /campus-setting → RecruitmentCampusSettingOut
- PUT /campus-setting → RecruitmentCampusSettingOut
- GET /nearby-kindergartens → RecruitmentNearbyKindergartensOut
- POST /market-intelligence/sync → RecruitmentMarketSyncResultOut
- GET /market-intelligence → RecruitmentMarketIntelligenceSnapshotOut

PII：
- 本檔無學生 / 家長姓名 / 電話。campus_name / kindergarten name 為公開
  商號（園所名稱、Google Places displayName / MOE CompetitorSchool
  school_name），非個資。
- 觸到 PII denylist 的「公開商務欄位」標 # pii-allow:：
  - campus_address / formatted_address — 本園/公開園所地址
  - phone — 教育部公開園所登記聯絡電話
  - owner_name — 教育部公開園所負責人欄位
"""

from __future__ import annotations

from typing import Any, Optional

from schemas._base import IvyBaseModel

# ──────────────────────────────────────────────────────────────────────
# GET /campus-setting + PUT /campus-setting → RecruitmentCampusSettingOut
# ──────────────────────────────────────────────────────────────────────


class RecruitmentCampusSettingOut(IvyBaseModel):
    """本園基本設定（地址 / 座標 / 通勤模式）。"""

    campus_name: str
    campus_address: Optional[str] = None  # pii-allow: 本園公開設立地址（非個資）
    campus_lat: Optional[float] = None
    campus_lng: Optional[float] = None
    travel_mode: str
    updated_at: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────
# GET /nearby-kindergartens → RecruitmentNearbyKindergartensOut
# ──────────────────────────────────────────────────────────────────────


class RecruitmentNearbyKindergartenOut(IvyBaseModel):
    """單筆鄰近幼兒園資訊（Google Places + MOE enrichment 合併）。"""

    place_id: Optional[str] = None
    name: Optional[str] = None
    formatted_address: Optional[str] = (
        None  # pii-allow: 公開園所地址（Google Places / 教育部公開資料）
    )
    lat: Optional[float] = None
    lng: Optional[float] = None
    primary_type: Optional[str] = None
    types: list[str] = []
    business_status: Optional[str] = None
    google_maps_uri: Optional[str] = None
    distance_km: Optional[float] = None
    rating: Optional[float] = None
    user_rating_count: Optional[int] = None
    # MOE enrichment 欄位（_enrich_from_moe / _empty_enrichment）
    db_id: Optional[int] = None
    school_type: Optional[str] = None
    pre_public_type: Optional[str] = None
    phone: Optional[str] = None  # pii-allow: 教育部公開園所登記聯絡電話（非個資）
    approved_capacity: Optional[int] = None
    monthly_fee: Optional[float] = None
    has_penalty: bool = False
    is_active: bool = True
    owner_name: Optional[str] = None  # pii-allow: 教育部公開園所登記負責人欄位
    approved_date: Optional[str] = None
    total_area_sqm: Optional[float] = None
    website: Optional[str] = None
    indoor_area_sqm: Optional[float] = None
    outdoor_area_sqm: Optional[float] = None
    floor: Optional[str] = None
    shuttle: Optional[str] = None
    has_after_school: bool = False
    source: Optional[str] = None


class RecruitmentNearbyQueryBoundsOut(IvyBaseModel):
    """前端傳入的視野 bounding box 回放（debug 用）。"""

    south: float
    west: float
    north: float
    east: float
    zoom: Optional[int] = None


class RecruitmentNearbyKindergartensOut(IvyBaseModel):
    """鄰近幼兒園查詢結果包。"""

    provider_available: bool
    provider_name: str
    total: int
    schools: list[RecruitmentNearbyKindergartenOut]
    message: Optional[str] = None
    query_bounds: Optional[RecruitmentNearbyQueryBoundsOut] = None


# ──────────────────────────────────────────────────────────────────────
# GET /market-intelligence → RecruitmentMarketIntelligenceSnapshotOut
# POST /market-intelligence/sync → RecruitmentMarketSyncResultOut（巢嵌 snapshot）
# ──────────────────────────────────────────────────────────────────────


class RecruitmentMarketDistrictRowOut(IvyBaseModel):
    """市場情報單一行政區彙總（lead + 人口 + 競品）。"""

    district: str
    town_code: Optional[str] = None
    lead_count_30d: int
    lead_count_90d: int
    deposit_rate_90d: Optional[float] = None  # 樣本<10 時 service 回 None（業主決議）
    avg_travel_minutes: Optional[float] = None
    population_density: Optional[float] = None
    population_0_6: Optional[int] = None
    data_completeness: str
    competitor_count: int = 0
    competitor_capacity: int = 0
    public_count: int = 0
    private_count: int = 0
    penalty_count: int = 0


class RecruitmentMarketIntelligenceSnapshotOut(IvyBaseModel):
    """市場情報快照。"""

    campus: RecruitmentCampusSettingOut
    districts: list[RecruitmentMarketDistrictRowOut]
    data_completeness: str
    synced_at: Optional[str] = None


class RecruitmentMarketSyncResultOut(IvyBaseModel):
    """sync 完成後回傳：彙總 + 快照。"""

    campus: RecruitmentCampusSettingOut
    target_county: Optional[str] = None
    hotspots_synced: int
    area_rows: int
    warning: Optional[Any] = None
    synced_at: Optional[str] = None
    snapshot: RecruitmentMarketIntelligenceSnapshotOut
