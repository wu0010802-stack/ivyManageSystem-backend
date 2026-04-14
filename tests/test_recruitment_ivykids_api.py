"""義華校官網報名 API / 同步回歸測試。"""

import os
import sys
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.recruitment import get_recruitment_stats, list_recruitment_records
from api.recruitment_ivykids import (
    IvykidsBackendSyncPayload,
    delete_recruitment_ivykids_backend_records,
    get_recruitment_ivykids_backend_status,
    get_recruitment_ivykids_stats,
    list_recruitment_ivykids_records,
    sync_recruitment_ivykids_backend,
)
from models.base import Base
from models.recruitment import (
    RecruitmentIvykidsRecord,
    RecruitmentSyncState,
    RecruitmentVisit,
)
from services import recruitment_ivykids_sync as ivykids_sync_service


@pytest.fixture
def recruitment_session_factory(tmp_path):
    db_path = tmp_path / "recruitment-ivykids-api.sqlite"
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


class TestRecruitmentIvykidsApi:
    def test_main_and_ivykids_apis_are_fully_separated(self, recruitment_session_factory):
        with recruitment_session_factory() as session:
            session.add(
                RecruitmentVisit(
                    month="115.04",
                    child_name="手動名單",
                    source="朋友介紹",
                    has_deposit=False,
                )
            )
            session.add(
                RecruitmentIvykidsRecord(
                    external_id="1001",
                    external_status="預約正常",
                    external_created_at="2026-04-10 09:15",
                    month="115.04",
                    visit_date="2026-04-12 10:30",
                    child_name="官網名單",
                    source="官網預約",
                    has_deposit=True,
                    enrolled=False,
                    transfer_term=False,
                )
            )
            session.commit()

        recruitment_stats = get_recruitment_stats(_=None)
        ivykids_stats = get_recruitment_ivykids_stats(_=None)
        recruitment_records = list_recruitment_records(
            month=None,
            grade=None,
            source=None,
            referrer=None,
            has_deposit=None,
            no_deposit_reason=None,
            keyword=None,
            page=1,
            page_size=50,
            _=None,
        )
        ivykids_records = list_recruitment_ivykids_records(
            month=None,
            source=None,
            page=1,
            page_size=50,
            _=None,
        )

        assert recruitment_stats["total_visit"] == 1
        assert recruitment_records["total"] == 1
        assert recruitment_records["records"][0]["child_name"] == "手動名單"

        assert ivykids_stats["total_visit"] == 1
        assert ivykids_stats["total_deposit"] == 1
        assert ivykids_records["total"] == 1
        assert ivykids_records["records"][0]["child_name"] == "官網名單"
        assert ivykids_records["records"][0]["external_status"] == "預約正常"
        assert ivykids_records["records"][0]["external_created_at"] == "2026-04-10 09:15"

    def test_sync_imports_into_dedicated_table_without_touching_manual_records(
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
                    source="手動建立",
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
                        birthday=date(2021, 5, 6),
                        grade="小班",
                        address="高雄市三民區民族一路100號",
                        district="三民區",
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
        assert result["inserted"] == 1
        assert result["updated"] == 0

        with recruitment_session_factory() as session:
            manual_rows = session.query(RecruitmentVisit).all()
            assert len(manual_rows) == 1
            assert manual_rows[0].source == "手動建立"
            assert manual_rows[0].address is None

            ivykids_rows = session.query(RecruitmentIvykidsRecord).all()
            assert len(ivykids_rows) == 1
            assert ivykids_rows[0].external_id == "1001"
            assert ivykids_rows[0].source == "官網預約"
            assert ivykids_rows[0].address == "高雄市三民區民族一路100號"

    def test_sync_updates_existing_ivykids_record_by_external_id(
        self,
        recruitment_session_factory,
        monkeypatch,
    ):
        with recruitment_session_factory() as session:
            session.add(
                RecruitmentIvykidsRecord(
                    external_id="1001",
                    external_status="待確認",
                    external_created_at="2026-04-09 08:00",
                    month="115.04",
                    child_name="小安",
                    source="舊來源",
                    has_deposit=False,
                    enrolled=False,
                    transfer_term=False,
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
            row = session.query(RecruitmentIvykidsRecord).one()
            assert row.external_status == "預約正常"
            assert row.external_created_at == "2026-04-10 09:15"
            assert row.source == "官網預約"
            assert row.address == "高雄市左營區明誠路100號"

    def test_sync_imports_cancelled_records_and_keeps_status_mark(
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
                        external_id="1002",
                        status="預約已取消",
                        visit_date="2026-04-15 下午場 14:30",
                        child_name="取消件名單",
                        phone="0912000333",
                        source="親友介紹",
                        created_at="2026-04-09 16:03:31",
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

        assert result["sync_success"] is True
        assert result["inserted"] == 1
        assert result["updated"] == 0
        assert result["skipped"] == 0

        with recruitment_session_factory() as session:
            row = session.query(RecruitmentIvykidsRecord).one()
            assert row.external_id == "1002"
            assert row.external_status == "預約已取消"
            assert row.child_name == "取消件名單"
            assert row.external_created_at == "2026-04-09 16:03:31"

    def test_delete_only_clears_ivykids_records_and_sync_state(self, recruitment_session_factory):
        with recruitment_session_factory() as session:
            session.add(RecruitmentVisit(month="115.05", child_name="手動名單", has_deposit=False))
            session.add(
                RecruitmentIvykidsRecord(
                    external_id="1001",
                    month="115.05",
                    child_name="官網名單",
                    has_deposit=False,
                    enrolled=False,
                    transfer_term=False,
                )
            )
            session.add(
                RecruitmentSyncState(
                    provider_name=ivykids_sync_service.IVYKIDS_BACKEND_SOURCE,
                    provider_label=ivykids_sync_service.IVYKIDS_PROVIDER_LABEL,
                    sync_in_progress=False,
                )
            )
            session.commit()

        result = delete_recruitment_ivykids_backend_records(_=None)

        assert result["deleted"] == 1
        assert result["reset_states"] == 1

        with recruitment_session_factory() as session:
            assert session.query(RecruitmentVisit).count() == 1
            assert session.query(RecruitmentIvykidsRecord).count() == 0
            assert session.query(RecruitmentSyncState).count() == 0

    def test_status_endpoint_returns_scheduler_and_last_sync_metadata(
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
                    provider_label=ivykids_sync_service.IVYKIDS_PROVIDER_LABEL,
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
        assert result["provider_name"] == ivykids_sync_service.IVYKIDS_BACKEND_SOURCE
        assert result["provider_label"] == ivykids_sync_service.IVYKIDS_PROVIDER_LABEL
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

    def test_stats_and_default_records_only_include_data_from_created_at_cutoff_onward(
        self,
        recruitment_session_factory,
    ):
        with recruitment_session_factory() as session:
            session.add_all(
                [
                    RecruitmentIvykidsRecord(
                        external_id="legacy-001",
                        external_created_at="2024-04-25 23:59:59",
                        month="113.05",
                        child_name="門檻前資料",
                        source="舊官網",
                        has_deposit=True,
                        enrolled=True,
                        transfer_term=False,
                    ),
                    RecruitmentIvykidsRecord(
                        external_id="cutoff-001",
                        external_created_at="2024-04-26 10:46:04",
                        month="113.04",
                        child_name="起算秒資料",
                        source="官網預約",
                        has_deposit=True,
                        enrolled=False,
                        transfer_term=False,
                    ),
                    RecruitmentIvykidsRecord(
                        external_id="current-001",
                        external_created_at="2024-05-01 09:00:00",
                        month="113.05",
                        child_name="五月資料",
                        source="官網預約",
                        has_deposit=False,
                        enrolled=False,
                        transfer_term=False,
                    ),
                    RecruitmentIvykidsRecord(
                        external_id="current-002",
                        external_created_at="2026-04-12 19:59:45",
                        month="115.04",
                        child_name="新資料",
                        source="官網預約",
                        has_deposit=True,
                        enrolled=False,
                        transfer_term=False,
                    ),
                ]
            )
            session.commit()

        stats = get_recruitment_ivykids_stats(_=None)
        records = list_recruitment_ivykids_records(
            month=None,
            source=None,
            page=1,
            page_size=50,
            _=None,
        )

        assert stats["total_visit"] == 3
        assert stats["total_deposit"] == 2
        assert stats["total_enrolled"] == 0
        assert [row["month"] for row in stats["by_month"]] == ["113.04", "113.05", "115.04"]

        assert records["total"] == 3
        assert [row["child_name"] for row in records["records"]] == ["新資料", "五月資料", "起算秒資料"]

    def test_sync_prunes_and_skips_records_before_created_at_cutoff(
        self,
        recruitment_session_factory,
        monkeypatch,
    ):
        with recruitment_session_factory() as session:
            session.add(
                RecruitmentIvykidsRecord(
                    external_id="old-existing",
                    external_status="預約時間已過",
                    external_created_at="2026-03-20 08:00:00",
                    month="115.03",
                    child_name="舊門檻資料",
                    source="官網預約",
                    has_deposit=False,
                    enrolled=False,
                    transfer_term=False,
                )
            )
            session.commit()

        monkeypatch.setenv("IVYKIDS_SYNC_CREATED_AT_CUTOFF", "2026-04-01 00:00:00")
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
                        external_id="old-fetched",
                        status="預約時間已過",
                        visit_date="2026-03-28 上午場 10:00",
                        child_name="門檻前資料",
                        phone="0912000111",
                        source="官網預約",
                        created_at="2026-03-28 09:00:00",
                        detail_url=None,
                        month="115.03",
                    ),
                    ivykids_sync_service.IvykidsBackendRecord(
                        external_id="new-fetched",
                        status="預約正常",
                        visit_date="2026-04-12 上午場 10:00",
                        child_name="門檻後資料",
                        phone="0912000222",
                        source="官網預約",
                        created_at="2026-04-12 19:59:45",
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

        assert result["sync_success"] is True
        assert result["inserted"] == 1
        assert result["updated"] == 0
        assert result["skipped"] == 1

        with recruitment_session_factory() as session:
            rows = session.query(RecruitmentIvykidsRecord).order_by(RecruitmentIvykidsRecord.external_id).all()
            assert [row.external_id for row in rows] == ["new-fetched"]
