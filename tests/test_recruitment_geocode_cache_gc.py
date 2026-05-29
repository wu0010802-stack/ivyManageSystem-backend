"""RecruitmentGeocodeCache 90d GC scheduler step"""

from datetime import datetime, timedelta

import pytest

from tests.test_recruitment_api import recruitment_session_factory  # noqa: F401
from models.recruitment import RecruitmentGeocodeCache


def test_cache_gc_removes_old_rows(recruitment_session_factory) -> None:
    from services.security_gc_scheduler import _gc_recruitment_geocode_cache

    with recruitment_session_factory() as s:
        # 91 天前 → 應被刪
        s.add(
            RecruitmentGeocodeCache(
                address="臺北市X路100巷",
                lat=25.0,
                lng=121.5,
                status="resolved",
                resolved_at=datetime.utcnow() - timedelta(days=91),
            )
        )
        # 89 天前 — 保留
        s.add(
            RecruitmentGeocodeCache(
                address="臺北市Y路200巷",
                lat=25.1,
                lng=121.6,
                status="resolved",
                resolved_at=datetime.utcnow() - timedelta(days=89),
            )
        )
        s.commit()

    with recruitment_session_factory() as s:
        deleted_count = _gc_recruitment_geocode_cache(s)
        s.commit()

    assert deleted_count == 1

    with recruitment_session_factory() as s:
        remaining = s.query(RecruitmentGeocodeCache).all()
        assert len(remaining) == 1
        assert remaining[0].address == "臺北市Y路200巷"


def test_cache_gc_skips_null_resolved_at(recruitment_session_factory) -> None:
    """resolved_at = NULL（pending/failed）不刪"""
    from services.security_gc_scheduler import _gc_recruitment_geocode_cache

    with recruitment_session_factory() as s:
        s.add(
            RecruitmentGeocodeCache(
                address="臺北市Z路",
                lat=None,
                lng=None,
                status="pending",
                resolved_at=None,
            )
        )
        s.commit()

    with recruitment_session_factory() as s:
        deleted = _gc_recruitment_geocode_cache(s)
        s.commit()

    assert deleted == 0


def test_cache_gc_keeps_boundary_exactly_90d(recruitment_session_factory) -> None:
    """剛好 90 天 — 不刪（> 才刪，邊界保留）"""
    from services.security_gc_scheduler import _gc_recruitment_geocode_cache

    with recruitment_session_factory() as s:
        s.add(
            RecruitmentGeocodeCache(
                address="臺北市E路100巷",
                lat=25.0,
                lng=121.5,
                status="resolved",
                resolved_at=datetime.utcnow() - timedelta(days=89, hours=23),
            )
        )
        s.commit()

    with recruitment_session_factory() as s:
        deleted = _gc_recruitment_geocode_cache(s)
        s.commit()

    assert deleted == 0
