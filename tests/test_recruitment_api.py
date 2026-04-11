"""招生統計 API / schema 回歸測試。"""

import asyncio
import os
import sys
import threading
from datetime import date, datetime, timedelta
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api import recruitment as recruitment_api
from api.recruitment import (
    CampusSettingPayload,
    ImportRecord,
    IvykidsBackendSyncPayload,
    MonthCreate,
    RecruitmentVisitCreate,
    RecruitmentVisitUpdate,
    get_recruitment_campus_setting,
    get_recruitment_address_hotspots,
    get_recruitment_ivykids_backend_status,
    get_recruitment_market_intelligence,
    get_recruitment_options,
    get_nearby_kindergartens,
    get_recruitment_stats,
    get_periods_summary,
    import_recruitment_records,
    list_recruitment_records,
    sync_recruitment_ivykids_backend,
    export_recruitment_stats,
    sync_recruitment_market_intelligence,
    sync_recruitment_address_hotspots,
    update_recruitment_campus_setting,
)
from models.base import Base
from models.recruitment import (
    RecruitmentAreaInsightCache,
    RecruitmentCampusSetting,
    RecruitmentGeocodeCache,
    RecruitmentPeriod,
    RecruitmentSyncState,
    RecruitmentVisit,
)
from services import recruitment_ivykids_sync as ivykids_sync_service


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


@pytest.fixture
def recruitment_client(recruitment_session_factory):
    app = FastAPI()
    app.include_router(recruitment_api.router)

    nearby_route = next(
        route
        for route in app.routes
        if getattr(route, "path", "") == "/api/recruitment/nearby-kindergartens"
    )
    for dependency in nearby_route.dependant.dependencies:
        app.dependency_overrides[dependency.call] = lambda: None

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


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


class TestIvykidsBackendSync:
    def test_fetch_backend_records_reads_multiple_pages_and_dedupes_ids(self, monkeypatch):
        page_one_html = """
        <html><body>
          <table id="sortable">
            <tr>
              <td>預約正常</td><td>2026-04-12 10:30</td><td>小安</td><td>--</td>
              <td>0912000111</td><td>官網預約</td><td>2026-04-10 09:15</td>
              <td><a href="form.php?id=1001">編輯</a></td>
            </tr>
          </table>
          <a href="https://www.ivykids.tw/manage/make_an_appointment/?page=2">2</a>
        </body></html>
        """
        page_two_html = """
        <html><body>
          <table id="sortable">
            <tr>
              <td>預約正常</td><td>2026-04-13 09:00</td><td>小寶</td><td>--</td>
              <td>0912000222</td><td>Google</td><td>2026-04-10 10:00</td>
              <td><a href="form.php?id=1002">編輯</a></td>
            </tr>
            <tr>
              <td>預約正常</td><td>2026-04-12 10:30</td><td>小安</td><td>--</td>
              <td>0912000111</td><td>官網預約</td><td>2026-04-10 09:15</td>
              <td><a href="form.php?id=1001">編輯</a></td>
            </tr>
          </table>
        </body></html>
        """

        class FakeResponse:
            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        class FakeSession:
            def __init__(self):
                self.headers = {}

            def post(self, url, data=None, timeout=None):
                return FakeResponse("ok")

            def get(self, url, timeout=None):
                if url == ivykids_sync_service.IVYKIDS_DATA_URL:
                    return FakeResponse(page_one_html)
                if url == "https://www.ivykids.tw/manage/make_an_appointment/?page=2":
                    return FakeResponse(page_two_html)
                raise AssertionError(f"unexpected url: {url}")

        monkeypatch.setattr(ivykids_sync_service, "_build_requests_session", lambda: FakeSession())
        monkeypatch.setattr(ivykids_sync_service, "_get_credentials", lambda: ("demo", "secret"))

        records, page_count = ivykids_sync_service.fetch_backend_records(max_pages=5)

        assert page_count == 2
        assert [row.external_id for row in records] == ["1002", "1001"]
        assert records[0].child_name == "小寶"
        assert records[1].month == "115.04"

    def test_parse_backend_record_detail_extracts_optional_fields(self):
        detail_html = """
        <html><body>
          <table>
            <tr><td>生日</td><td><input name="birthday" value="2021-05-06" /></td></tr>
            <tr><td>適讀班級</td><td><select name="grade"><option>幼幼班</option><option selected>小班</option></select></td></tr>
            <tr><td>地址</td><td><textarea name="address">高雄市三民區民族一路100號</textarea></td></tr>
            <tr><td>行政區</td><td><input name="district" value="三民區" /></td></tr>
            <tr><td>介紹者</td><td><input name="referrer" value="王主任" /></td></tr>
            <tr><td>備註</td><td><textarea name="notes">需先追蹤排隊狀況</textarea></td></tr>
            <tr><td>電訪後家長回應</td><td><textarea name="parent_response">六月後再決定</textarea></td></tr>
            <tr><td>收預繳人員</td><td><input name="deposit_collector" value="Ruby老師" /></td></tr>
            <tr><td>是否預繳</td><td><input name="has_deposit" value="是" /></td></tr>
            <tr><td>是否報到</td><td><input name="enrolled" value="否" /></td></tr>
            <tr><td>轉其他學期</td><td><input name="transfer_term" value="是" /></td></tr>
          </table>
        </body></html>
        """

        detail = ivykids_sync_service.parse_backend_record_detail(detail_html)

        assert detail["birthday"] == date(2021, 5, 6)
        assert detail["grade"] == "小班"
        assert detail["address"] == "高雄市三民區民族一路100號"
        assert detail["district"] == "三民區"
        assert detail["referrer"] == "王主任"
        assert detail["notes"] == "需先追蹤排隊狀況"
        assert detail["parent_response"] == "六月後再決定"
        assert detail["deposit_collector"] == "Ruby老師"
        assert detail["has_deposit"] is True
        assert detail["enrolled"] is False
        assert detail["transfer_term"] is True

    def test_parse_backend_record_detail_returns_none_for_missing_fields(self):
        detail = ivykids_sync_service.parse_backend_record_detail("<html><body><form></form></body></html>")

        assert detail["birthday"] is None
        assert detail["grade"] is None
        assert detail["address"] is None
        assert detail["notes"] is None
        assert detail["has_deposit"] is None

    def test_sync_imports_records_and_attaches_external_identity(
        self,
        recruitment_session_factory,
        monkeypatch,
    ):
        monkeypatch.setattr(ivykids_sync_service, "sync_configured", lambda: True)
        monkeypatch.setattr(ivykids_sync_service, "_login_session", lambda _session: None)
        monkeypatch.setattr(
            ivykids_sync_service,
            "_build_requests_session",
            lambda: type(
                "FakeSession",
                (),
                {
                    "__enter__": lambda self: self,
                    "__exit__": lambda self, exc_type, exc, tb: None,
                    "close": lambda self: None,
                },
            )(),
        )
        monkeypatch.setattr(
            ivykids_sync_service,
            "fetch_backend_records",
            lambda max_pages=20, http_session=None, authenticated=False: (
                [
                    ivykids_sync_service.IvykidsBackendRecord(
                        external_id="1001",
                        status="預約正常",
                        visit_date="2026-04-12 10:30",
                        child_name="小安",
                        phone="0912000111",
                        source="官網預約",
                        created_at="2026-04-10 09:15",
                        detail_url=None,
                        month="115.04",
                        birthday=date(2021, 5, 6),
                        grade="小班",
                        address="高雄市三民區民族一路100號",
                        district="三民區",
                        notes="六月再聯繫",
                        parent_response="媽媽表示會再評估",
                    ),
                    ivykids_sync_service.IvykidsBackendRecord(
                        external_id="1002",
                        status="已取消",
                        visit_date="2026-04-13 09:00",
                        child_name="小寶",
                        phone="0912000222",
                        source="Google",
                        created_at="2026-04-10 10:00",
                        detail_url=None,
                        month="115.04",
                    ),
                ],
                1,
            ),
        )
        monkeypatch.setattr(
            ivykids_sync_service,
            "enrich_backend_records",
            lambda records, http_session: list(records),
        )

        result = sync_recruitment_ivykids_backend(
            IvykidsBackendSyncPayload(max_pages=5),
            _=None,
        )

        assert result["provider_available"] is True
        assert result["sync_success"] is True
        assert result["total_fetched"] == 2
        assert result["inserted"] == 1
        assert result["updated"] == 0
        assert result["skipped"] == 1
        assert result["page_count"] == 1

        with recruitment_session_factory() as session:
            rows = session.query(RecruitmentVisit).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.month == "115.04"
            assert row.child_name == "小安"
            assert row.phone == "0912000111"
            assert row.source == "官網預約"
            assert row.has_deposit is False
            assert row.external_source == ivykids_sync_service.IVYKIDS_BACKEND_SOURCE
            assert row.external_id == "1001"
            assert row.external_status == "預約正常"
            assert row.birthday == date(2021, 5, 6)
            assert row.grade == "小班"
            assert row.address == "高雄市三民區民族一路100號"
            assert row.notes == "六月再聯繫"
            assert row.parent_response == "媽媽表示會再評估"

    def test_sync_updates_existing_manual_record_when_signature_matches(
        self,
        recruitment_session_factory,
        monkeypatch,
    ):
        with recruitment_session_factory() as session:
            session.add(
                RecruitmentVisit(
                    month="115.04",
                    visit_date="115.04.12",
                    child_name="小安",
                    phone="0912000111",
                    source="舊來源",
                    has_deposit=False,
                )
            )
            session.commit()

        monkeypatch.setattr(ivykids_sync_service, "sync_configured", lambda: True)
        monkeypatch.setattr(ivykids_sync_service, "_login_session", lambda _session: None)
        monkeypatch.setattr(
            ivykids_sync_service,
            "_build_requests_session",
            lambda: type(
                "FakeSession",
                (),
                {
                    "__enter__": lambda self: self,
                    "__exit__": lambda self, exc_type, exc, tb: None,
                    "close": lambda self: None,
                },
            )(),
        )
        monkeypatch.setattr(
            ivykids_sync_service,
            "fetch_backend_records",
            lambda max_pages=20, http_session=None, authenticated=False: (
                [
                    ivykids_sync_service.IvykidsBackendRecord(
                        external_id="1001",
                        status="預約正常",
                        visit_date="2026-04-12 10:30",
                        child_name="小安",
                        phone="0912000111",
                        source="官網預約",
                        created_at="2026-04-10 09:15",
                        detail_url=None,
                        month="115.04",
                        address="高雄市左營區明誠路100號",
                    ),
                ],
                1,
            ),
        )
        monkeypatch.setattr(
            ivykids_sync_service,
            "enrich_backend_records",
            lambda records, http_session: list(records),
        )

        result = sync_recruitment_ivykids_backend(
            IvykidsBackendSyncPayload(max_pages=5),
            _=None,
        )

        assert result["sync_success"] is True
        assert result["inserted"] == 0
        assert result["updated"] == 1

        with recruitment_session_factory() as session:
            rows = session.query(RecruitmentVisit).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.external_id == "1001"
            assert row.external_source == ivykids_sync_service.IVYKIDS_BACKEND_SOURCE
            assert row.external_status == "預約正常"
            assert row.source == "官網預約"
            assert row.address == "高雄市左營區明誠路100號"

    def test_sync_returns_provider_unavailable_when_credentials_missing(
        self,
        recruitment_session_factory,
        monkeypatch,
    ):
        monkeypatch.setattr(ivykids_sync_service, "sync_configured", lambda: False)

        result = sync_recruitment_ivykids_backend(
            IvykidsBackendSyncPayload(max_pages=5),
            _=None,
        )

        assert result["provider_available"] is False
        assert result["sync_success"] is False
        assert result["inserted"] == 0
        assert result["updated"] == 0
        assert "IVYKIDS_USERNAME" in (result["message"] or "")

    def test_sync_failure_updates_sync_state(
        self,
        recruitment_session_factory,
        monkeypatch,
    ):
        monkeypatch.setattr(ivykids_sync_service, "sync_configured", lambda: True)
        monkeypatch.setattr(ivykids_sync_service, "_login_session", lambda _session: None)
        monkeypatch.setattr(
            ivykids_sync_service,
            "_build_requests_session",
            lambda: type(
                "FakeSession",
                (),
                {
                    "__enter__": lambda self: self,
                    "__exit__": lambda self, exc_type, exc, tb: None,
                    "close": lambda self: None,
                },
            )(),
        )

        def _raise_fetch(*args, **kwargs):
            raise RuntimeError("登入失敗")

        monkeypatch.setattr(ivykids_sync_service, "fetch_backend_records", _raise_fetch)

        result = sync_recruitment_ivykids_backend(
            IvykidsBackendSyncPayload(max_pages=5),
            _=None,
        )

        assert result["provider_available"] is True
        assert result["sync_success"] is False
        assert result["message"] == "登入失敗"

        with recruitment_session_factory() as session:
            state = session.query(RecruitmentSyncState).filter_by(
                provider_name=ivykids_sync_service.IVYKIDS_BACKEND_SOURCE
            ).one()
            assert state.sync_in_progress is False
            assert state.last_sync_status == "failed"
            assert state.last_sync_message == "登入失敗"

    def test_sync_busy_guard_returns_busy_state(
        self,
        recruitment_session_factory,
        monkeypatch,
    ):
        monkeypatch.setattr(ivykids_sync_service, "sync_configured", lambda: True)

        test_lock = threading.Lock()
        test_lock.acquire()
        monkeypatch.setattr(ivykids_sync_service, "_SYNC_LOCK", test_lock)

        try:
            result = sync_recruitment_ivykids_backend(
                IvykidsBackendSyncPayload(max_pages=5),
                _=None,
            )
        finally:
            test_lock.release()

        assert result["provider_available"] is True
        assert result["sync_success"] is False
        assert result["sync_in_progress"] is True
        assert "進行中" in result["message"]

    def test_get_status_returns_scheduler_and_last_sync_metadata(
        self,
        recruitment_session_factory,
        monkeypatch,
    ):
        monkeypatch.setattr(ivykids_sync_service, "sync_configured", lambda: True)
        monkeypatch.setattr(ivykids_sync_service, "scheduler_requested", lambda: True)
        monkeypatch.setattr(ivykids_sync_service, "scheduler_configured", lambda: True)
        monkeypatch.setattr(ivykids_sync_service, "get_sync_interval_minutes", lambda: 10)

        synced_at = datetime(2026, 4, 12, 9, 30, 0)
        with recruitment_session_factory() as session:
            session.add(
                RecruitmentSyncState(
                    provider_name=ivykids_sync_service.IVYKIDS_BACKEND_SOURCE,
                    provider_label="義華校官網",
                    sync_in_progress=False,
                    last_synced_at=synced_at,
                    last_sync_status="success",
                    last_sync_message="義華校官網同步完成",
                    last_sync_counts='{"inserted": 2, "updated": 1, "skipped": 0, "total_fetched": 3, "page_count": 1}',
                )
            )
            session.commit()

        result = get_recruitment_ivykids_backend_status(_=None)

        assert result["provider_available"] is True
        assert result["scheduler_enabled"] is True
        assert result["sync_interval_minutes"] == 10
        assert result["last_synced_at"] == synced_at.isoformat()
        assert result["last_sync_counts"] == {
            "inserted": 2,
            "updated": 1,
            "skipped": 0,
            "total_fetched": 3,
            "page_count": 1,
        }


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

    def test_source_analysis_groups_configured_aliases_and_supports_grouped_filter(
        self,
        recruitment_session_factory,
    ):
        with recruitment_session_factory() as session:
            session.add_all([
                RecruitmentVisit(
                    month="115.05",
                    child_name="童年甲",
                    source="童年綠地 19-1",
                    referrer="Ruby",
                    has_deposit=True,
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="童年乙",
                    source="二人同行",
                    referrer="Ruby",
                    has_deposit=False,
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="童年丙",
                    source="Ruby老師",
                    referrer="Amy",
                    has_deposit=True,
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="分校甲",
                    source="國際校介紹",
                    referrer="Amy",
                    has_deposit=False,
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="分校乙",
                    source="仁武校介紹",
                    referrer="Amy",
                    has_deposit=True,
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="分校丙",
                    source="崇德校介紹",
                    referrer="Amy",
                    has_deposit=False,
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="網路甲",
                    source="網路",
                    referrer="Amy",
                    has_deposit=False,
                ),
            ])
            session.commit()

        stats = get_recruitment_stats(_=None)

        assert stats["by_source"] == [
            {"source": "童年綠地", "visit": 3, "deposit": 2},
            {"source": "分校介紹", "visit": 3, "deposit": 1},
            {"source": "網路", "visit": 1, "deposit": 0},
        ]
        assert stats["top_source_names"] == ["童年綠地", "分校介紹", "網路"]

        cross_rows = {
            row["referrer"]: row
            for row in stats["referrer_source_cross"]["referrers"]
        }
        assert cross_rows["Amy"]["sources"] == {
            "童年綠地": 1,
            "分校介紹": 3,
            "網路": 1,
        }
        assert cross_rows["Ruby"]["sources"] == {
            "童年綠地": 2,
            "分校介紹": 0,
            "網路": 0,
        }

        options = get_recruitment_options(_=None)
        assert "分校介紹" in options["sources"]
        assert "崇德校介紹" not in options["sources"]

        branch_result = list_recruitment_records(
            month=None,
            grade=None,
            source="分校介紹",
            referrer=None,
            has_deposit=None,
            no_deposit_reason=None,
            keyword=None,
            page=1,
            page_size=50,
            _=None,
        )
        assert branch_result["total"] == 3
        assert {row["source"] for row in branch_result["records"]} == {
            "國際校介紹",
            "仁武校介紹",
            "崇德校介紹",
        }

        grouped_result = list_recruitment_records(
            month=None,
            grade=None,
            source="童年綠地",
            referrer=None,
            has_deposit=None,
            no_deposit_reason=None,
            keyword=None,
            page=1,
            page_size=50,
            _=None,
        )
        assert grouped_result["total"] == 3
        assert {row["source"] for row in grouped_result["records"]} == {
            "童年綠地 19-1",
            "二人同行",
            "Ruby老師",
        }

        exact_result = list_recruitment_records(
            month=None,
            grade=None,
            source="二人同行",
            referrer=None,
            has_deposit=None,
            no_deposit_reason=None,
            keyword=None,
            page=1,
            page_size=50,
            _=None,
        )
        assert exact_result["total"] == 1
        assert exact_result["records"][0]["child_name"] == "童年乙"

    def test_stats_include_decision_summary_alerts_action_queue_and_reference_month(
        self,
        recruitment_session_factory,
    ):
        now = datetime.now()
        with recruitment_session_factory() as session:
            session.add_all([
                RecruitmentVisit(
                    month="115.04",
                    child_name="前月已註冊1",
                    district="三民區",
                    source="介紹",
                    has_deposit=True,
                    enrolled=True,
                    transfer_term=False,
                    created_at=now - timedelta(days=40),
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="前月已註冊2",
                    district="三民區",
                    source="介紹",
                    has_deposit=True,
                    enrolled=True,
                    transfer_term=False,
                    created_at=now - timedelta(days=39),
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="前月已預繳3",
                    district="左營區",
                    source="網路",
                    has_deposit=True,
                    enrolled=False,
                    transfer_term=False,
                    created_at=now - timedelta(days=38),
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="前月已預繳4",
                    district="左營區",
                    source="網路",
                    has_deposit=True,
                    enrolled=False,
                    transfer_term=False,
                    created_at=now - timedelta(days=37),
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="前月未預繳",
                    district="左營區",
                    source="網路",
                    has_deposit=False,
                    enrolled=False,
                    transfer_term=False,
                    created_at=now - timedelta(days=36),
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="本月高潛力1",
                    district="三民區",
                    source="網路",
                    has_deposit=False,
                    enrolled=False,
                    transfer_term=False,
                    no_deposit_reason="時程未到／仍在觀望",
                    created_at=now - timedelta(days=20),
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="本月高潛力2",
                    district="三民區",
                    source="網路",
                    has_deposit=False,
                    enrolled=False,
                    transfer_term=False,
                    no_deposit_reason="時程未到／仍在觀望",
                    created_at=now - timedelta(days=19),
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="本月高潛力3",
                    district="三民區",
                    source="網路",
                    has_deposit=False,
                    enrolled=False,
                    transfer_term=False,
                    no_deposit_reason="課程／環境仍在評估",
                    created_at=now - timedelta(days=18),
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="本月高潛力4",
                    district="三民區",
                    source="網路",
                    has_deposit=False,
                    enrolled=False,
                    transfer_term=False,
                    no_deposit_reason="課程／環境仍在評估",
                    created_at=now - timedelta(days=17),
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="本月高潛力5",
                    district="三民區",
                    source="網路",
                    has_deposit=False,
                    enrolled=False,
                    transfer_term=False,
                    no_deposit_reason="時程未到／仍在觀望",
                    created_at=now - timedelta(days=16),
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="本月其他來源1",
                    district="鳳山區",
                    source="介紹",
                    has_deposit=True,
                    enrolled=True,
                    transfer_term=False,
                    created_at=now - timedelta(days=12),
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="本月其他來源2",
                    district="鳳山區",
                    source="介紹",
                    has_deposit=True,
                    enrolled=False,
                    transfer_term=False,
                    created_at=now - timedelta(days=10),
                ),
            ])
            session.commit()

        stats = get_recruitment_stats(reference_month="115.05", _=None)

        assert stats["reference_month"] == "115.05"
        assert stats["decision_summary"]["current_month"]["visit"] == 7
        assert stats["decision_summary"]["current_month"]["deposit"] == 2
        assert stats["decision_summary"]["rolling_30d"]["visit"] == 7
        assert stats["decision_summary"]["rolling_90d"]["visit"] == 12
        assert stats["decision_summary"]["ytd"]["visit"] == 12
        assert stats["funnel_snapshot"] == {
            "visit": 7,
            "deposit": 2,
            "enrolled": 1,
            "transfer_term": 0,
            "effective_deposit": 2,
            "pending_deposit": 1,
        }
        assert stats["month_over_month"]["current_month"] == "115.05"
        assert stats["month_over_month"]["previous_month"] == "115.04"
        assert stats["month_over_month"]["visit_to_deposit_rate"]["delta"] == -51.4
        assert stats["month_over_month"]["visit_to_enrolled_rate"]["delta"] == -25.7

        alert_codes = {item["code"] for item in stats["alerts"]}
        assert {"FUNNEL_DROP", "HIGH_POTENTIAL_BACKLOG", "SOURCE_IMBALANCE"} <= alert_codes
        assert any(item["target_tab"] == "nodeposit" for item in stats["alerts"])
        assert any(item["target_tab"] == "detail" for item in stats["alerts"])
        assert any(item["target_tab"] == "area" for item in stats["top_action_queue"])
        assert any(item["target_tab"] == "detail" for item in stats["top_action_queue"])
        assert any(item["target_tab"] == "nodeposit" for item in stats["top_action_queue"])

    def test_no_deposit_analysis_summary_and_priority_overdue_filters(
        self,
        recruitment_session_factory,
    ):
        now = datetime.now()
        with recruitment_session_factory() as session:
            session.add_all([
                RecruitmentVisit(
                    month="115.05",
                    child_name="高潛力逾期1",
                    grade="小班",
                    has_deposit=False,
                    no_deposit_reason="時程未到／仍在觀望",
                    created_at=now - timedelta(days=20),
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="高潛力逾期2",
                    grade="小班",
                    has_deposit=False,
                    no_deposit_reason="課程／環境仍在評估",
                    created_at=now - timedelta(days=18),
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="高潛力未逾期",
                    grade="中班",
                    has_deposit=False,
                    no_deposit_reason="課程／環境仍在評估",
                    created_at=now - timedelta(days=5),
                ),
                RecruitmentVisit(
                    month="115.05",
                    child_name="冷名單",
                    grade="大班",
                    has_deposit=False,
                    no_deposit_reason="已有其他就學選項／比較他校",
                    created_at=now - timedelta(days=95),
                ),
            ])
            session.commit()

        result = recruitment_api.get_no_deposit_analysis(
            priority="high",
            overdue_days=14,
            cold_only=None,
            reason=None,
            grade=None,
            page=1,
            page_size=50,
            _=None,
        )

        assert result["summary"] == {
            "high_potential_count": 3,
            "overdue_followup_count": 3,
            "cold_count": 1,
        }
        assert result["total"] == 2
        assert {row["child_name"] for row in result["records"]} == {"高潛力逾期1", "高潛力逾期2"}

    def test_stats_export_adds_decision_summary_sheet(self, recruitment_session_factory):
        with recruitment_session_factory() as session:
            session.add_all([
                RecruitmentVisit(month="115.04", child_name="小安", has_deposit=True, enrolled=True),
                RecruitmentVisit(month="115.05", child_name="小寶", has_deposit=False, enrolled=False),
            ])
            session.commit()

        response = export_recruitment_stats(reference_month="115.05", _=None)

        async def _collect_body():
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            return b"".join(chunks)

        body = asyncio.run(_collect_body())
        workbook = load_workbook(BytesIO(body))

        assert workbook.sheetnames[0] == "決策摘要"
        sheet = workbook["決策摘要"]
        assert sheet["A1"].value == "招生決策摘要"
        assert any("115.05" in str(sheet.cell(row=row, column=1).value or "") for row in range(1, 12))


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
        assert result["provider_name"] in {"nominatim", "google", "tgos"}
        assert result["geocoded_hotspots"] == 0
        assert result["pending_hotspots"] == 2
        assert result["remaining_hotspots"] == 2
        assert result["failed_hotspots"] == 0
        assert result["stale_hotspots"] == 0
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
                "matched_address": None,
                "google_place_id": None,
                "town_code": None,
                "town_name": None,
                "county_name": None,
                "land_use_label": None,
                "travel_minutes": None,
                "travel_distance_km": None,
                "data_quality": "partial",
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
                "matched_address": None,
                "google_place_id": None,
                "town_code": None,
                "town_name": None,
                "county_name": None,
                "land_use_label": None,
                "travel_minutes": None,
                "travel_distance_km": None,
                "data_quality": "partial",
            },
        ]

    def test_address_hotspots_counts_all_cached_rows_beyond_display_limit(self, recruitment_session_factory):
        with recruitment_session_factory() as session:
            session.add_all([
                RecruitmentVisit(
                    month="115.04",
                    child_name="小安",
                    address="高雄市三民區民族一路100號",
                    district="三民區",
                    has_deposit=True,
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="小寶",
                    address="高雄市鳳山區光遠路50號",
                    district="鳳山區",
                    has_deposit=False,
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="小晴",
                    address="高雄市左營區自由路1號",
                    district="左營區",
                    has_deposit=False,
                ),
            ])
            session.add_all([
                RecruitmentGeocodeCache(
                    address="高雄市三民區民族一路100號",
                    district="三民區",
                    provider="google",
                    status="resolved",
                    lat=22.6461,
                    lng=120.3209,
                    google_place_id="google-place-1",
                ),
                RecruitmentGeocodeCache(
                    address="高雄市鳳山區光遠路50號",
                    district="鳳山區",
                    provider="google",
                    status="resolved",
                    lat=22.6321,
                    lng=120.3565,
                    google_place_id="google-place-2",
                ),
                RecruitmentGeocodeCache(
                    address="高雄市左營區自由路1號",
                    district="左營區",
                    provider="google",
                    status="resolved",
                    lat=22.6782,
                    lng=120.3081,
                    google_place_id="google-place-3",
                ),
            ])
            session.commit()

        result = get_recruitment_address_hotspots(limit=2, _=None)

        assert len(result["hotspots"]) == 2
        assert result["total_hotspots"] == 3
        assert result["geocoded_hotspots"] == 3
        assert result["pending_hotspots"] == 0
        assert result["remaining_hotspots"] == 0
        assert result["failed_hotspots"] == 0

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

        monkeypatch.setattr(recruitment_api.market_service, "market_provider_available", lambda: True)
        monkeypatch.setattr(recruitment_api.market_service, "current_market_provider", lambda: "google")
        monkeypatch.setattr(
            recruitment_api.market_service,
            "resolve_address_metadata",
            lambda address, campus=None: {
                "provider": "google",
                "lat": 22.6461,
                "lng": 120.3209,
                "formatted_address": f"TW {address}",
                "matched_address": f"TW {address}",
                "google_place_id": "google-place-1",
                "town_code": "64000010",
                "town_name": "三民區",
                "county_name": "高雄市",
                "land_use_label": "住宅區",
                "travel_minutes": 8.5,
                "travel_distance_km": 3.2,
                "data_quality": "estimated",
            },
        )

        result = sync_recruitment_address_hotspots(batch_size=5, limit=10, _=None)

        assert result["sync_mode"] == "incremental"
        assert result["attempted"] == 1
        assert result["synced"] == 1
        assert result["failed"] == 0
        assert result["skipped"] == 0
        assert result["geocoded_hotspots"] == 1
        assert result["pending_hotspots"] == 0
        assert result["remaining_hotspots"] == 0
        assert result["stale_hotspots"] == 0
        assert result["hotspots"][0]["lat"] == 22.6461
        assert result["hotspots"][0]["provider"] == "google"
        assert result["hotspots"][0]["google_place_id"] == "google-place-1"
        assert result["hotspots"][0]["town_code"] == "64000010"
        assert result["hotspots"][0]["travel_minutes"] == 8.5

        with recruitment_session_factory() as session:
            cached = session.query(RecruitmentGeocodeCache).one()
            assert cached.address == "高雄市三民區民族一路100號"
            assert cached.status == "resolved"
            assert cached.lat == 22.6461
            assert cached.lng == 120.3209
            assert cached.google_place_id == "google-place-1"
            assert cached.town_code == "64000010"
            assert cached.travel_distance_km == 3.2

    def test_sync_address_hotspots_processes_all_hotspots_beyond_display_limit(
        self,
        recruitment_session_factory,
        monkeypatch,
    ):
        with recruitment_session_factory() as session:
            session.add_all([
                RecruitmentVisit(
                    month="115.04",
                    child_name="小安",
                    address="高雄市三民區民族一路100號",
                    district="三民區",
                    has_deposit=True,
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="小寶",
                    address="高雄市鳳山區光遠路50號",
                    district="鳳山區",
                    has_deposit=False,
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="小晴",
                    address="高雄市左營區自由路1號",
                    district="左營區",
                    has_deposit=False,
                ),
            ])
            session.commit()

        monkeypatch.setattr(recruitment_api.market_service, "market_provider_available", lambda: True)
        monkeypatch.setattr(recruitment_api.market_service, "current_market_provider", lambda: "google")
        monkeypatch.setattr(
            recruitment_api.market_service,
            "resolve_address_metadata",
            lambda address, campus=None: {
                "provider": "google",
                "lat": 22.6,
                "lng": 120.3,
                "formatted_address": f"Google {address}",
                "matched_address": f"Google {address}",
                "google_place_id": f"place:{address}",
                "town_code": "64000010",
                "town_name": "測試行政區",
                "county_name": "高雄市",
                "land_use_label": "住宅區",
                "travel_minutes": 9.5,
                "travel_distance_km": 4.2,
                "data_quality": "complete",
            },
        )

        result = sync_recruitment_address_hotspots(batch_size=5, limit=1, _=None)

        assert len(result["hotspots"]) == 1
        assert result["attempted"] == 3
        assert result["synced"] == 3
        assert result["failed"] == 0
        assert result["geocoded_hotspots"] == 3
        assert result["pending_hotspots"] == 0
        assert result["remaining_hotspots"] == 0

        with recruitment_session_factory() as session:
            assert session.query(RecruitmentGeocodeCache).count() == 3

    def test_address_hotspots_reports_stale_google_upgrade_count(self, recruitment_session_factory):
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
            session.add(
                RecruitmentGeocodeCache(
                    address="高雄市三民區民族一路100號",
                    district="三民區",
                    provider="nominatim",
                    status="resolved",
                    lat=22.6461,
                    lng=120.3209,
                )
            )
            session.commit()

        result = get_recruitment_address_hotspots(limit=10, _=None)

        assert result["geocoded_hotspots"] == 1
        assert result["stale_hotspots"] == 1
        assert result["hotspots"][0]["provider"] == "nominatim"
        assert result["hotspots"][0]["google_place_id"] is None

    def test_resync_google_only_targets_non_google_or_failed_cache(self, recruitment_session_factory, monkeypatch):
        with recruitment_session_factory() as session:
            session.add_all([
                RecruitmentVisit(
                    month="115.04",
                    child_name="小安",
                    address="高雄市三民區民族一路100號",
                    district="三民區",
                    has_deposit=True,
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="小寶",
                    address="高雄市三民區澄清路88號",
                    district="三民區",
                    has_deposit=False,
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="小晴",
                    address="高雄市左營區自由路1號",
                    district="左營區",
                    has_deposit=False,
                ),
            ])
            session.add_all([
                RecruitmentGeocodeCache(
                    address="高雄市三民區民族一路100號",
                    district="三民區",
                    provider="google",
                    status="resolved",
                    lat=22.6461,
                    lng=120.3209,
                    google_place_id="google-place-current",
                ),
                RecruitmentGeocodeCache(
                    address="高雄市三民區澄清路88號",
                    district="三民區",
                    provider="nominatim",
                    status="resolved",
                    lat=22.6401,
                    lng=120.3301,
                ),
                RecruitmentGeocodeCache(
                    address="高雄市左營區自由路1號",
                    district="左營區",
                    provider=None,
                    status="failed",
                ),
            ])
            session.commit()

        monkeypatch.setattr(recruitment_api.market_service, "market_provider_available", lambda: True)
        monkeypatch.setattr(recruitment_api.market_service, "current_market_provider", lambda: "google")

        resolved_addresses = []

        def fake_resolve(address, campus=None):
            resolved_addresses.append(address)
            return {
                "provider": "google",
                "lat": 22.6,
                "lng": 120.3,
                "formatted_address": f"Google {address}",
                "matched_address": f"Google {address}",
                "google_place_id": f"place:{address}",
                "town_code": "64000010",
                "town_name": "三民區",
                "county_name": "高雄市",
                "land_use_label": "住宅區",
                "travel_minutes": 7.5,
                "travel_distance_km": 2.8,
                "data_quality": "complete",
            }

        monkeypatch.setattr(recruitment_api.market_service, "resolve_address_metadata", fake_resolve)

        result = sync_recruitment_address_hotspots(
            batch_size=10,
            limit=10,
            sync_mode="resync_google",
            _=None,
        )

        assert result["sync_mode"] == "resync_google"
        assert result["attempted"] == 2
        assert result["synced"] == 2
        assert result["failed"] == 0
        assert result["skipped"] == 1
        assert result["stale_hotspots"] == 0
        assert resolved_addresses == [
            "高雄市三民區澄清路88號",
            "高雄市左營區自由路1號",
        ]

        with recruitment_session_factory() as session:
            current = {
                row.address: row
                for row in session.query(RecruitmentGeocodeCache).all()
            }
            assert current["高雄市三民區民族一路100號"].google_place_id == "google-place-current"
            assert current["高雄市三民區澄清路88號"].provider == "google"
            assert current["高雄市三民區澄清路88號"].google_place_id == "place:高雄市三民區澄清路88號"
            assert current["高雄市左營區自由路1號"].provider == "google"


class TestMarketIntelligence:
    def test_campus_setting_upsert_and_query(self, recruitment_session_factory):
        payload = CampusSettingPayload(
            campus_name="本園",
            campus_address="高雄市三民區民族一路100號",
            campus_lat=22.6461,
            campus_lng=120.3209,
            travel_mode="driving",
        )

        update_recruitment_campus_setting(payload, _=None)
        result = get_recruitment_campus_setting(_=None)

        assert result["campus_name"] == "本園"
        assert result["travel_mode"] == "driving"
        assert result["campus_lat"] == 22.6461

    def test_market_intelligence_returns_raw_metrics(self, recruitment_session_factory):
        with recruitment_session_factory() as session:
            session.add_all([
                RecruitmentVisit(
                    month="115.04",
                    child_name="小安",
                    district="三民區",
                    address="高雄市三民區民族一路100號",
                    has_deposit=True,
                ),
                RecruitmentVisit(
                    month="115.04",
                    child_name="小寶",
                    district="三民區",
                    address="高雄市三民區澄清路88號",
                    has_deposit=False,
                ),
                RecruitmentGeocodeCache(
                    address="高雄市三民區民族一路100號",
                    district="三民區",
                    town_code="64000010",
                    travel_minutes=9.0,
                    status="resolved",
                ),
                RecruitmentAreaInsightCache(
                    district="三民區",
                    town_code="64000010",
                    population_density=1234.5,
                    population_0_6=456,
                    data_completeness="partial",
                ),
                RecruitmentCampusSetting(
                    campus_name="本園",
                    campus_address="高雄市三民區民族一路100號",
                    campus_lat=22.6461,
                    campus_lng=120.3209,
                    travel_mode="driving",
                ),
            ])
            session.commit()

        snapshot = get_recruitment_market_intelligence(_=None)

        assert snapshot["campus"]["campus_name"] == "本園"
        assert snapshot["districts"][0]["district"] == "三民區"
        assert snapshot["districts"][0]["lead_count_90d"] == 2
        assert snapshot["districts"][0]["deposit_rate_90d"] == 50.0
        assert snapshot["districts"][0]["population_density"] == 1234.5

    def test_market_sync_keeps_cached_snapshot_when_no_external_indexes(self, recruitment_session_factory, monkeypatch):
        with recruitment_session_factory() as session:
            session.add(
                RecruitmentAreaInsightCache(
                    district="三民區",
                    town_code="64000010",
                    population_density=1234.5,
                    data_completeness="cached",
                )
            )
            session.add(
                RecruitmentCampusSetting(
                    campus_name="本園",
                    campus_address="高雄市三民區民族一路100號",
                    campus_lat=22.6461,
                    campus_lng=120.3209,
                    travel_mode="driving",
                )
            )
            session.commit()

        monkeypatch.setattr(recruitment_api.market_service, "load_population_density_index", lambda: {})
        monkeypatch.setattr(recruitment_api.market_service, "load_population_age_index", lambda: {})

        result = sync_recruitment_market_intelligence(hotspot_limit=200, _=None)

        assert "snapshot" in result
        assert result["snapshot"]["districts"][0]["data_completeness"] == "cached"


class TestNearbyKindergartens:
    def test_nearby_kindergartens_returns_deduped_places_and_query_bounds(
        self,
        recruitment_session_factory,
        monkeypatch,
    ):
        with recruitment_session_factory() as session:
            session.add(
                RecruitmentCampusSetting(
                    campus_name="本園",
                    campus_address="高雄市三民區民族一路100號",
                    campus_lat=22.6461,
                    campus_lng=120.3209,
                    travel_mode="driving",
                )
            )
            session.commit()

        requests = []

        def fake_query(payload, *, field_mask):
            requests.append((payload, field_mask))
            if payload.get("pageToken") == "token-2":
                return {
                    "places": [
                        {
                            "id": "place-2",
                            "displayName": {"text": "重複幼兒園"},
                            "formattedAddress": "高雄市三民區澄清路88號",
                            "location": {"latitude": 22.6501, "longitude": 120.3309},
                            "primaryType": "preschool",
                            "types": ["preschool", "school"],
                            "businessStatus": "OPERATIONAL",
                            "googleMapsUri": "https://maps.google.com/?cid=2",
                        },
                        {
                            "id": "place-3",
                            "displayName": {"text": "第三間幼兒園"},
                            "formattedAddress": "高雄市左營區自由一路1號",
                            "location": {"latitude": 22.6782, "longitude": 120.3081},
                            "primaryType": "preschool",
                            "types": ["preschool", "school"],
                            "businessStatus": "OPERATIONAL",
                            "googleMapsUri": "https://maps.google.com/?cid=3",
                        },
                    ]
                }

            return {
                "places": [
                    {
                        "id": "place-1",
                        "displayName": {"text": "本園旁幼兒園"},
                        "formattedAddress": "高雄市三民區民族一路100號",
                        "location": {"latitude": 22.6461, "longitude": 120.3209},
                        "primaryType": "preschool",
                        "types": ["preschool", "school"],
                        "businessStatus": "OPERATIONAL",
                        "googleMapsUri": "https://maps.google.com/?cid=1",
                    },
                    {
                        "id": "place-2",
                        "displayName": {"text": "重複幼兒園"},
                        "formattedAddress": "高雄市三民區澄清路88號",
                        "location": {"latitude": 22.6501, "longitude": 120.3309},
                        "primaryType": "preschool",
                        "types": ["preschool", "school"],
                        "businessStatus": "OPERATIONAL",
                        "googleMapsUri": "https://maps.google.com/?cid=2",
                    },
                ],
                "nextPageToken": "token-2",
            }

        monkeypatch.setattr(recruitment_api.market_service, "_google_places_api_available", lambda: True)
        monkeypatch.setattr(recruitment_api.market_service, "_query_google_places_text", fake_query)

        result = get_nearby_kindergartens(
            south=22.62,
            west=120.29,
            north=22.69,
            east=120.35,
            zoom=13,
            _=None,
        )

        assert result["provider_available"] is True
        assert result["provider_name"] == "google"
        assert result["query_bounds"] == {
            "south": 22.62,
            "west": 120.29,
            "north": 22.69,
            "east": 120.35,
            "zoom": 13,
        }
        assert result["total"] == 3
        assert [school["place_id"] for school in result["schools"]] == [
            "place-1",
            "place-2",
            "place-3",
        ]
        assert result["schools"][0]["distance_km"] == pytest.approx(0.0, abs=0.01)
        assert result["schools"][1]["distance_km"] > result["schools"][0]["distance_km"]
        assert len(requests) == 2
        assert requests[0][0]["textQuery"] == "幼兒園"
        assert requests[0][0]["includedType"] == "preschool"
        assert requests[0][0]["strictTypeFiltering"] is True
        assert requests[0][0]["locationRestriction"]["rectangle"] == {
            "low": {"latitude": 22.62, "longitude": 120.29},
            "high": {"latitude": 22.69, "longitude": 120.35},
        }
        assert requests[1][0]["pageToken"] == "token-2"

    def test_nearby_kindergartens_reports_provider_unavailable(
        self,
        recruitment_session_factory,
        monkeypatch,
    ):
        with recruitment_session_factory() as session:
            session.add(
                RecruitmentCampusSetting(
                    campus_name="本園",
                    campus_address="高雄市三民區民族一路100號",
                    campus_lat=22.6461,
                    campus_lng=120.3209,
                    travel_mode="driving",
                )
            )
            session.commit()

        monkeypatch.setattr(recruitment_api.market_service, "_google_places_api_available", lambda: False)

        result = get_nearby_kindergartens(
            south=22.62,
            west=120.29,
            north=22.69,
            east=120.35,
            zoom=13,
            _=None,
        )

        assert result["provider_available"] is False
        assert result["provider_name"] == "google"
        assert result["total"] == 0
        assert result["schools"] == []
        assert "Places API" in result["message"]

    def test_nearby_kindergartens_rejects_invalid_query_bounds(self, recruitment_client):
        response = recruitment_client.get(
            "/api/recruitment/nearby-kindergartens",
            params={
                "south": 95,
                "west": 120.29,
                "north": 22.69,
                "east": 120.35,
                "zoom": 13,
            },
        )

        assert response.status_code == 422

    def test_nearby_kindergartens_distance_uses_campus_center(
        self,
        recruitment_session_factory,
        monkeypatch,
    ):
        with recruitment_session_factory() as session:
            session.add(
                RecruitmentCampusSetting(
                    campus_name="本園",
                    campus_address="高雄市三民區民族一路100號",
                    campus_lat=22.6461,
                    campus_lng=120.3209,
                    travel_mode="driving",
                )
            )
            session.commit()

        monkeypatch.setattr(recruitment_api.market_service, "_google_places_api_available", lambda: True)
        monkeypatch.setattr(
            recruitment_api.market_service,
            "_query_google_places_text",
            lambda payload, *, field_mask: {
                "places": [
                    {
                        "id": "place-1",
                        "displayName": {"text": "同位置幼兒園"},
                        "formattedAddress": "高雄市三民區民族一路100號",
                        "location": {"latitude": 22.6461, "longitude": 120.3209},
                        "primaryType": "preschool",
                        "types": ["preschool"],
                        "businessStatus": "OPERATIONAL",
                        "googleMapsUri": "https://maps.google.com/?cid=1",
                    },
                    {
                        "id": "place-2",
                        "displayName": {"text": "另一間幼兒園"},
                        "formattedAddress": "高雄市三民區建國一路10號",
                        "location": {"latitude": 22.6401, "longitude": 120.3159},
                        "primaryType": "preschool",
                        "types": ["preschool"],
                        "businessStatus": "OPERATIONAL",
                        "googleMapsUri": "https://maps.google.com/?cid=2",
                    },
                ]
            },
        )

        result = get_nearby_kindergartens(
            south=22.62,
            west=120.29,
            north=22.69,
            east=120.35,
            zoom=13,
            _=None,
        )

        assert result["schools"][0]["distance_km"] == pytest.approx(0.0, abs=0.01)
        assert result["schools"][1]["distance_km"] > 0
