"""招生統計 API / schema 回歸測試。"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api import recruitment as recruitment_api
from api.recruitment import (
    ImportRecord,
    MonthCreate,
    RecruitmentVisitCreate,
    RecruitmentVisitUpdate,
    get_recruitment_address_hotspots,
    get_recruitment_stats,
    get_periods_summary,
    import_recruitment_records,
    sync_recruitment_address_hotspots,
)
from models.base import Base
from models.recruitment import RecruitmentGeocodeCache, RecruitmentPeriod, RecruitmentVisit


@pytest.fixture
def recruitment_session_factory(tmp_path):
    db_path = tmp_path / "recruitment-api.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    try:
        yield session_factory
    finally:
        base_module._engine = old_engine
        base_module._SessionFactory = old_session_factory
        engine.dispose()


class TestRecruitmentMonthNormalization:
    def test_schema_normalizes_month_to_zero_padded_format(self):
        create_payload = RecruitmentVisitCreate(month="115.4", child_name="小安")
        update_payload = RecruitmentVisitUpdate(month="115.4")
        month_payload = MonthCreate(month="115.4")

        assert create_payload.month == "115.04"
        assert update_payload.month == "115.04"
        assert month_payload.month == "115.04"

    def test_import_normalizes_month_before_persisting(self, recruitment_session_factory):
        result = import_recruitment_records(
            [ImportRecord(**{"月份": "115.4", "幼生姓名": "小安"})],
            _=None,
        )

        assert result == {"inserted": 1, "skipped": 0}

        with recruitment_session_factory() as session:
            record = session.query(RecruitmentVisit).one()
            assert record.month == "115.04"


class TestPeriodsSummary:
    def test_by_grade_only_counts_visits_within_defined_periods(self, recruitment_session_factory):
        with recruitment_session_factory() as session:
            session.add(
                RecruitmentPeriod(
                    period_name="114.09.16~115.03.15",
                    visit_count=2,
                    deposit_count=1,
                    enrolled_count=1,
                    effective_deposit_count=1,
                    sort_order=1,
                )
            )
            session.add_all([
                RecruitmentVisit(
                    month="114.09",
                    child_name="小安",
                    grade="小班",
                    has_deposit=True,
                    enrolled=True,
                ),
                RecruitmentVisit(
                    month="115.03",
                    child_name="小寶",
                    grade="中班",
                    has_deposit=False,
                    enrolled=False,
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="小晴",
                    grade="大班",
                    has_deposit=True,
                    enrolled=True,
                ),
            ])
            session.commit()

        summary = get_periods_summary(_=None)

        assert summary["total_visit"] == 2
        assert summary["by_grade"] == [
            {
                "grade": "小班",
                "visit": 1,
                "deposit": 1,
                "enrolled": 1,
                "visit_to_deposit_rate": 100.0,
                "visit_to_enrolled_rate": 100.0,
                "deposit_to_enrolled_rate": 100.0,
            },
            {
                "grade": "中班",
                "visit": 1,
                "deposit": 0,
                "enrolled": 0,
                "visit_to_deposit_rate": 0.0,
                "visit_to_enrolled_rate": 0.0,
                "deposit_to_enrolled_rate": 0,
            },
        ]


class TestRecruitmentStats:
    def test_live_stats_include_enrollment_funnel_counts_and_rates(self, recruitment_session_factory):
        with recruitment_session_factory() as session:
            session.add_all([
                RecruitmentVisit(
                    month="115.04",
                    child_name="小安",
                    has_deposit=True,
                    enrolled=True,
                    transfer_term=False,
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="小寶",
                    has_deposit=True,
                    enrolled=False,
                    transfer_term=False,
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="小晴",
                    has_deposit=False,
                    enrolled=False,
                    transfer_term=False,
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="小樂",
                    has_deposit=True,
                    enrolled=False,
                    transfer_term=True,
                ),
            ])
            session.commit()

        stats = get_recruitment_stats(_=None)

        assert stats["total_visit"] == 4
        assert stats["total_deposit"] == 3
        assert stats["total_enrolled"] == 1
        assert stats["total_transfer_term"] == 1
        assert stats["total_pending_deposit"] == 1
        assert stats["total_effective_deposit"] == 2
        assert stats["visit_to_deposit_rate"] == 75.0
        assert stats["visit_to_enrolled_rate"] == 25.0
        assert stats["deposit_to_enrolled_rate"] == pytest.approx(33.3, abs=0.1)
        assert stats["effective_to_enrolled_rate"] == 50.0
        assert stats["monthly"] == [
            {
                "month": "115.04",
                "visit": 2,
                "deposit": 2,
                "enrolled": 1,
                "transfer_term": 0,
                "pending_deposit": 1,
                "effective_deposit": 2,
                "visit_to_deposit_rate": 100.0,
                "visit_to_enrolled_rate": 50.0,
                "deposit_to_enrolled_rate": 50.0,
                "effective_to_enrolled_rate": 50.0,
                "chuannian_visit": 0,
                "chuannian_deposit": 0,
            },
            {
                "month": "115.05",
                "visit": 2,
                "deposit": 1,
                "enrolled": 0,
                "transfer_term": 1,
                "pending_deposit": 0,
                "effective_deposit": 0,
                "visit_to_deposit_rate": 50.0,
                "visit_to_enrolled_rate": 0.0,
                "deposit_to_enrolled_rate": 0.0,
                "effective_to_enrolled_rate": 0,
                "chuannian_visit": 0,
                "chuannian_deposit": 0,
            },
        ]
        assert stats["by_year"] == [
            {
                "year": "115",
                "visit": 4,
                "deposit": 3,
                "enrolled": 1,
                "transfer_term": 1,
                "pending_deposit": 1,
                "effective_deposit": 2,
                "visit_to_deposit_rate": 75.0,
                "visit_to_enrolled_rate": 25.0,
                "deposit_to_enrolled_rate": pytest.approx(33.3, abs=0.1),
                "effective_to_enrolled_rate": 50.0,
                "chuannian_visit": 0,
                "chuannian_deposit": 0,
            },
        ]


class TestAddressHotspots:
    def test_groups_records_by_full_address_and_falls_back_to_district_from_address(
        self,
        recruitment_session_factory,
    ):
        with recruitment_session_factory() as session:
            session.add_all([
                RecruitmentVisit(
                    month="115.04",
                    child_name="小安",
                    address="高雄市三民區民族一路100號",
                    district=None,
                    has_deposit=True,
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="小寶",
                    address="高雄市三民區民族一路100號",
                    district="三民區",
                    has_deposit=False,
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="小晴",
                    address="高雄市鳳山區光遠路50號",
                    district="鳳山區",
                    has_deposit=True,
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="小樂",
                    address="   ",
                    district="左營區",
                    has_deposit=False,
                ),
            ])
            session.commit()

        result = get_recruitment_address_hotspots(limit=10, _=None)

        assert result["records_with_address"] == 3
        assert result["total_hotspots"] == 2
        assert result["provider_available"] is True
        assert result["provider_name"] in {"nominatim", "google"}
        assert result["geocoded_hotspots"] == 0
        assert result["pending_hotspots"] == 2
        assert result["failed_hotspots"] == 0
        assert result["hotspots"] == [
            {
                "address": "高雄市三民區民族一路100號",
                "district": "三民區",
                "visit": 2,
                "deposit": 1,
                "lat": None,
                "lng": None,
                "geocode_status": "pending",
                "provider": None,
                "formatted_address": None,
            },
            {
                "address": "高雄市鳳山區光遠路50號",
                "district": "鳳山區",
                "visit": 1,
                "deposit": 1,
                "lat": None,
                "lng": None,
                "geocode_status": "pending",
                "provider": None,
                "formatted_address": None,
            },
        ]

    def test_sync_address_hotspots_persists_geocode_cache(self, recruitment_session_factory, monkeypatch):
        with recruitment_session_factory() as session:
            session.add(
                RecruitmentVisit(
                    month="115.04",
                    child_name="小安",
                    address="高雄市三民區民族一路100號",
                    district="三民區",
                    has_deposit=True,
                )
            )
            session.commit()

        monkeypatch.setattr(recruitment_api, "can_geocode", lambda: True)
        monkeypatch.setattr(recruitment_api, "current_geocoding_provider", lambda: "google")
        monkeypatch.setattr(
            recruitment_api,
            "geocode_address",
            lambda address: {
                "provider": "google",
                "lat": 22.6461,
                "lng": 120.3209,
                "formatted_address": f"TW {address}",
            },
        )

        result = sync_recruitment_address_hotspots(batch_size=5, limit=10, _=None)

        assert result["synced"] == 1
        assert result["failed"] == 0
        assert result["geocoded_hotspots"] == 1
        assert result["pending_hotspots"] == 0
        assert result["hotspots"][0]["lat"] == 22.6461
        assert result["hotspots"][0]["provider"] == "google"

        with recruitment_session_factory() as session:
            cached = session.query(RecruitmentGeocodeCache).one()
            assert cached.address == "高雄市三民區民族一路100號"
            assert cached.status == "resolved"
            assert cached.lat == 22.6461
            assert cached.lng == 120.3209
