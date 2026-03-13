"""官方國定假日 / 補班日同步回歸測試。"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import services.official_calendar as official_calendar_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.events import router as events_router
from api.portal.calendar import router as portal_calendar_router
from models.database import (
    Base,
    Holiday,
    OfficialCalendarSync,
    SchoolEvent,
    User,
    WorkdayOverride,
)
from services.salary.utils import get_working_days
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def official_calendar_env(tmp_path):
    db_path = tmp_path / "official-calendar.sqlite"
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
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(events_router)
    app.include_router(portal_calendar_router, prefix="/api/portal")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, username: str, permissions: int, password: str = "TempPass123") -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=permissions,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient, username: str, password: str = "TempPass123"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


class TestOfficialCalendarParser:
    def test_parse_official_calendar_csv_splits_holidays_and_makeup_days(self):
        csv_text = """西元日期,星期,是否放假,備註
20250127,一,2,
20250208,六,0,補行上班
20250228,五,2,和平紀念日
20250301,六,2,
20251004,六,2,中秋節
"""

        holidays, makeup_days = official_calendar_module._parse_official_calendar_csv(csv_text)

        assert [item["date"].isoformat() for item in holidays] == [
            "2025-01-27",
            "2025-02-28",
            "2025-10-04",
        ]
        assert [item["name"] for item in holidays] == [
            "國定假日",
            "和平紀念日",
            "中秋節",
        ]
        assert makeup_days == [{
            "date": date(2025, 2, 8),
            "name": "補班日",
            "description": "補行上班",
        }]


class TestOfficialCalendarSync:
    def test_sync_upserts_holidays_and_makeup_days(self, official_calendar_env, monkeypatch):
        _, session_factory = official_calendar_env
        monkeypatch.setattr(
            official_calendar_module,
            "_get_resource_metadata",
            lambda year: {"download_url": "https://example.com/2026.csv", "modified_at": "v1", "description": "115年"},
        )
        monkeypatch.setattr(
            official_calendar_module,
            "_fetch_official_calendar_entries",
            lambda year: (
                [{"date": date(2026, 2, 27), "name": "二二八補假", "description": "和平紀念日補假"}],
                [{"date": date(2026, 2, 7), "name": "補班日", "description": "補行上班"}],
                {"download_url": "https://example.com/2026.csv", "modified_at": "v1", "description": "115年"},
            ),
        )

        with session_factory() as session:
            result = official_calendar_module.ensure_official_calendar_synced(session, 2026)

            assert result["status"] == "synced"
            holiday = session.query(Holiday).filter(Holiday.date == date(2026, 2, 27)).one()
            makeup = session.query(WorkdayOverride).filter(WorkdayOverride.date == date(2026, 2, 7)).one()
            sync = session.query(OfficialCalendarSync).filter(OfficialCalendarSync.sync_year == 2026).one()

            assert holiday.source == official_calendar_module.OFFICIAL_SOURCE
            assert holiday.source_year == 2026
            assert makeup.source == official_calendar_module.OFFICIAL_SOURCE
            assert makeup.source_year == 2026
            assert sync.is_synced is True
            assert sync.source_modified_at == "v1"

    def test_resync_deactivates_removed_official_entries(self, official_calendar_env, monkeypatch):
        _, session_factory = official_calendar_env
        metadata = {"modified_at": "v1"}
        payload = {
            "holidays": [{"date": date(2026, 2, 27), "name": "舊補假", "description": "舊資料"}],
            "makeup_days": [{"date": date(2026, 2, 7), "name": "補班日", "description": "舊資料"}],
        }

        monkeypatch.setattr(
            official_calendar_module,
            "_get_resource_metadata",
            lambda year: {"download_url": "https://example.com/2026.csv", "modified_at": metadata["modified_at"], "description": "115年"},
        )

        def fake_fetch(_year):
            return (
                payload["holidays"],
                payload["makeup_days"],
                {"download_url": "https://example.com/2026.csv", "modified_at": metadata["modified_at"], "description": "115年"},
            )

        monkeypatch.setattr(official_calendar_module, "_fetch_official_calendar_entries", fake_fetch)

        with session_factory() as session:
            official_calendar_module.ensure_official_calendar_synced(session, 2026)

            metadata["modified_at"] = "v2"
            payload["holidays"] = [{"date": date(2026, 10, 1), "name": "新假日", "description": "新版資料"}]
            payload["makeup_days"] = []

            result = official_calendar_module.ensure_official_calendar_synced(session, 2026)

            assert result["status"] == "synced"
            old_holiday = session.query(Holiday).filter(Holiday.date == date(2026, 2, 27)).one()
            new_holiday = session.query(Holiday).filter(Holiday.date == date(2026, 10, 1)).one()
            old_makeup = session.query(WorkdayOverride).filter(WorkdayOverride.date == date(2026, 2, 7)).one()

            assert old_holiday.is_active is False
            assert new_holiday.is_active is True
            assert old_makeup.is_active is False

    def test_sync_reuses_existing_manual_holiday_on_same_date(self, official_calendar_env, monkeypatch):
        _, session_factory = official_calendar_env
        monkeypatch.setattr(
            official_calendar_module,
            "_get_resource_metadata",
            lambda year: {"download_url": "https://example.com/2026.csv", "modified_at": "v1", "description": "115年"},
        )
        monkeypatch.setattr(
            official_calendar_module,
            "_fetch_official_calendar_entries",
            lambda year: (
                [{"date": date(2026, 1, 1), "name": "開國紀念日", "description": "開國紀念日"}],
                [],
                {"download_url": "https://example.com/2026.csv", "modified_at": "v1", "description": "115年"},
            ),
        )

        with session_factory() as session:
            session.add(
                Holiday(
                    date=date(2026, 1, 1),
                    name="元旦",
                    description="手動建立",
                    is_active=True,
                    source="manual",
                    source_year=2026,
                )
            )
            session.commit()

            result = official_calendar_module.ensure_official_calendar_synced(session, 2026)

            assert result["status"] == "synced"
            holidays = session.query(Holiday).filter(Holiday.date == date(2026, 1, 1)).all()
            assert len(holidays) == 1
            assert holidays[0].name == "開國紀念日"
            assert holidays[0].source == official_calendar_module.OFFICIAL_SOURCE
            assert holidays[0].source_year == 2026

    def test_sync_failure_without_cache_returns_warning(self, official_calendar_env, monkeypatch):
        _, session_factory = official_calendar_env
        monkeypatch.setattr(
            official_calendar_module,
            "_get_resource_metadata",
            lambda year: (_ for _ in ()).throw(RuntimeError("official source offline")),
        )

        with session_factory() as session:
            result = official_calendar_module.ensure_official_calendar_synced(session, 2026)

            assert result["status"] == "warning"
            assert result["used_cache"] is False
            assert "official source offline" in result["warning"]

    def test_sync_failure_uses_local_cache_when_available(self, official_calendar_env, monkeypatch):
        _, session_factory = official_calendar_env
        monkeypatch.setattr(
            official_calendar_module,
            "_get_resource_metadata",
            lambda year: {"download_url": "https://example.com/2026.csv", "modified_at": "v1", "description": "115年"},
        )
        monkeypatch.setattr(
            official_calendar_module,
            "_fetch_official_calendar_entries",
            lambda year: (
                [{"date": date(2026, 2, 27), "name": "二二八補假", "description": "和平紀念日補假"}],
                [],
                {"download_url": "https://example.com/2026.csv", "modified_at": "v1", "description": "115年"},
            ),
        )

        with session_factory() as session:
            official_calendar_module.ensure_official_calendar_synced(session, 2026)

        monkeypatch.setattr(
            official_calendar_module,
            "_get_resource_metadata",
            lambda year: (_ for _ in ()).throw(RuntimeError("temporary timeout")),
        )

        with session_factory() as session:
            result = official_calendar_module.ensure_official_calendar_synced(session, 2026)

            assert result["status"] == "cached"
            assert result["used_cache"] is True
            assert "本地快取" in result["warning"]


class TestCalendarFeedApi:
    def test_calendar_feed_auto_syncs_and_returns_read_only_official_events(self, official_calendar_env, monkeypatch):
        client, session_factory = official_calendar_env
        with session_factory() as session:
            _create_user(session, "calendar_admin", Permission.CALENDAR)
            session.add(
                SchoolEvent(
                    title="園務會議",
                    event_date=date(2026, 2, 9),
                    event_type="meeting",
                    is_active=True,
                )
            )
            session.commit()

        monkeypatch.setattr(
            official_calendar_module,
            "_get_resource_metadata",
            lambda year: {"download_url": "https://example.com/2026.csv", "modified_at": "v1", "description": "115年"},
        )
        monkeypatch.setattr(
            official_calendar_module,
            "_fetch_official_calendar_entries",
            lambda year: (
                [{"date": date(2026, 2, 27), "name": "二二八補假", "description": "和平紀念日補假"}],
                [{"date": date(2026, 2, 7), "name": "補班日", "description": "補行上班"}],
                {"download_url": "https://example.com/2026.csv", "modified_at": "v1", "description": "115年"},
            ),
        )

        login_res = _login(client, "calendar_admin")
        assert login_res.status_code == 200

        res = client.get("/api/events/calendar-feed", params={"year": 2026, "month": 2})
        assert res.status_code == 200

        data = res.json()
        assert data["official_sync"]["status"] == "synced"
        assert len(data["events"]) == 3

        holiday_event = next(item for item in data["events"] if item["official_kind"] == "holiday")
        makeup_event = next(item for item in data["events"] if item["official_kind"] == "makeup_workday")
        manual_event = next(item for item in data["events"] if item["official_kind"] is None)

        assert holiday_event["is_official"] is True
        assert holiday_event["is_read_only"] is True
        assert holiday_event["event_type"] == "holiday"
        assert makeup_event["is_official"] is True
        assert makeup_event["event_type"] == "makeup_workday"
        assert manual_event["is_official"] is False
        assert manual_event["is_read_only"] is False

        with session_factory() as session:
            assert session.query(Holiday).filter(Holiday.date == date(2026, 2, 27)).count() == 1
            assert session.query(WorkdayOverride).filter(WorkdayOverride.date == date(2026, 2, 7)).count() == 1

    def test_portal_calendar_uses_same_merged_feed(self, official_calendar_env, monkeypatch):
        client, session_factory = official_calendar_env
        with session_factory() as session:
            _create_user(session, "portal_teacher", 0)
            session.add(
                SchoolEvent(
                    title="親師座談",
                    event_date=date(2026, 2, 12),
                    event_type="activity",
                    is_active=True,
                )
            )
            session.commit()

        monkeypatch.setattr(
            official_calendar_module,
            "_get_resource_metadata",
            lambda year: {"download_url": "https://example.com/2026.csv", "modified_at": "v1", "description": "115年"},
        )
        monkeypatch.setattr(
            official_calendar_module,
            "_fetch_official_calendar_entries",
            lambda year: (
                [{"date": date(2026, 2, 27), "name": "二二八補假", "description": "和平紀念日補假"}],
                [{"date": date(2026, 2, 7), "name": "補班日", "description": "補行上班"}],
                {"download_url": "https://example.com/2026.csv", "modified_at": "v1", "description": "115年"},
            ),
        )

        login_res = _login(client, "portal_teacher")
        assert login_res.status_code == 200

        res = client.get("/api/portal/calendar", params={"year": 2026, "month": 2})
        assert res.status_code == 200

        data = res.json()
        assert data["official_sync"]["status"] == "synced"
        assert len(data["events"]) == 3
        assert any(item["official_kind"] == "holiday" for item in data["events"])
        assert any(item["official_kind"] == "makeup_workday" for item in data["events"])
        assert any(item["official_kind"] is None and item["title"] == "親師座談" for item in data["events"])


class TestWorkingDayRules:
    def test_get_working_days_counts_makeup_saturday_and_excludes_weekday_holiday(self, official_calendar_env):
        _, session_factory = official_calendar_env

        with session_factory() as session:
            session.add(Holiday(date=date(2026, 2, 27), name="補假", is_active=True))
            session.add(WorkdayOverride(date=date(2026, 2, 7), name="補班日", is_active=True))
            session.commit()

            baseline = sum(1 for day in range(1, 29) if date(2026, 2, day).weekday() < 5)

            assert get_working_days(2026, 2, session=session) == baseline
