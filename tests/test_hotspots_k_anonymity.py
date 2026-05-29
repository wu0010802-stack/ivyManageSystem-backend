"""hotspots 100m grid bucket + K=5 k-anonymity suppression"""

from datetime import datetime

import pytest

from tests.test_recruitment_api import recruitment_session_factory  # noqa: F401
from utils.taipei_time import now_taipei_naive
from api.recruitment.hotspots import _build_address_hotspots_response
from models.recruitment import RecruitmentVisit, RecruitmentGeocodeCache


def _seed_lane(
    session,
    lane_addr: str,
    lat: float,
    lng: float,
    count: int,
    district: str = "文山區",
):
    """Seed N visits at one lane (different door numbers) + cache row at truncated lane."""
    for i in range(count):
        session.add(
            RecruitmentVisit(
                month="115.05",
                child_name=f"K{i}-{lane_addr}",
                grade="幼兒",
                address=f"{lane_addr}{i+1}號",  # full address with door number
                district=district,
                geocoding_consent_at=now_taipei_naive(),
            )
        )
    # Cache uses truncated key (per new contract)
    existing = (
        session.query(RecruitmentGeocodeCache)
        .filter_by(address=lane_addr)
        .one_or_none()
    )
    if not existing:
        session.add(
            RecruitmentGeocodeCache(
                address=lane_addr,
                lat=lat,
                lng=lng,
                status="resolved",
                district=district,
                resolved_at=now_taipei_naive(),
            )
        )


def test_buckets_suppress_below_k(recruitment_session_factory) -> None:
    """K=5: bucket visit_count=4 應被 suppress；count=8 應 render"""
    with recruitment_session_factory() as s:
        _seed_lane(s, "臺北市文山區A路100巷", 25.014, 121.567, count=4)  # 4 < K=5
        _seed_lane(s, "臺北市文山區B路200巷", 25.040, 121.590, count=8)  # 8 >= K=5
        s.commit()

    with recruitment_session_factory() as s:
        result = _build_address_hotspots_response(s, limit=200, dataset_scope=None)

    buckets = result["buckets"]
    assert len(buckets) == 1, f"應 1 bucket render，實際 {len(buckets)}"
    assert buckets[0]["visit_count"] == 8

    residual = result["district_residual_visits"]
    assert residual.get("文山區") == 4, f"4 visit 應進 residual，實際 {residual}"


def test_buckets_use_density_weighted_center(recruitment_session_factory) -> None:
    """center_lat/lng 用 avg() 密度加權"""
    with recruitment_session_factory() as s:
        _seed_lane(s, "臺北市信義區X路100巷", 25.030, 121.567, count=5)
        s.commit()

    with recruitment_session_factory() as s:
        result = _build_address_hotspots_response(s, limit=200, dataset_scope=None)

    buckets = result["buckets"]
    assert len(buckets) == 1
    # Single cache row → center == cache lat/lng
    assert abs(buckets[0]["center_lat"] - 25.030) < 0.001
    assert abs(buckets[0]["center_lng"] - 121.567) < 0.001


def test_district_residual_aggregates_multiple_suppressed(
    recruitment_session_factory,
) -> None:
    """3 個 bucket 都 < K → residual 應加總"""
    with recruitment_session_factory() as s:
        _seed_lane(s, "臺北市文山區A路1巷", 25.014, 121.567, count=2, district="文山區")
        _seed_lane(s, "臺北市文山區B路2巷", 25.030, 121.580, count=3, district="文山區")
        _seed_lane(s, "臺北市文山區C路3巷", 25.040, 121.590, count=4, district="文山區")
        s.commit()

    with recruitment_session_factory() as s:
        result = _build_address_hotspots_response(s, limit=200, dataset_scope=None)

    assert result["buckets"] == [] or len(result["buckets"]) == 0
    residual = result["district_residual_visits"]
    assert residual.get("文山區") == 2 + 3 + 4
