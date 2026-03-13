"""高成本報表快取回歸測試。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Classroom, ReportSnapshot
from services.activity_service import ActivityService
from services.dashboard_query_service import DashboardQueryService
from services.report_cache_service import report_cache_service
from services.student_attendance_report import build_monthly_attendance_report
import services.student_attendance_report as student_report_module


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "report-cache.sqlite"
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

    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        base_module._engine = old_engine
        base_module._SessionFactory = old_session_factory
        engine.dispose()


def test_report_cache_service_reuses_fresh_snapshot(db_session):
    calls = []

    def builder():
        calls.append("build")
        return {"value": 42}

    first = report_cache_service.get_or_build(
        db_session,
        category="unit_test_cache",
        ttl_seconds=300,
        params={"scope": "same"},
        builder=builder,
    )
    second = report_cache_service.get_or_build(
        db_session,
        category="unit_test_cache",
        ttl_seconds=300,
        params={"scope": "same"},
        builder=builder,
    )

    assert first == {"value": 42}
    assert second == {"value": 42}
    assert calls == ["build"]
    assert db_session.query(ReportSnapshot).count() == 1


def test_activity_stats_are_served_from_cached_snapshot(db_session, monkeypatch):
    service = ActivityService()
    calls = []

    monkeypatch.setattr(
        service,
        "_compute_stats_summary",
        lambda session: calls.append("compute") or {"totalRegistrations": 1},
    )

    first = service.get_stats_summary(db_session)
    second = service.get_stats_summary(db_session)

    assert first == second == {"totalRegistrations": 1}
    assert calls == ["compute"]


def test_student_monthly_report_uses_cached_snapshot(db_session, monkeypatch):
    db_session.add(Classroom(name="向日葵班", is_active=True))
    db_session.commit()
    classroom = db_session.query(Classroom).filter(Classroom.name == "向日葵班").first()

    calls = []
    monkeypatch.setattr(
        student_report_module,
        "_compute_monthly_attendance_report",
        lambda session, classroom_id, year, month: calls.append((classroom_id, year, month)) or {"classroom_id": classroom_id, "year": year, "month": month},
    )

    first = build_monthly_attendance_report(db_session, classroom.id, 2026, 3)
    second = build_monthly_attendance_report(db_session, classroom.id, 2026, 3)

    assert first == second == {"classroom_id": classroom.id, "year": 2026, "month": 3}
    assert calls == [(classroom.id, 2026, 3)]


def test_home_student_attendance_summary_uses_cached_snapshot(db_session, monkeypatch):
    service = DashboardQueryService()
    calls = []
    target_date = date(2026, 3, 13)

    monkeypatch.setattr(
        service,
        "_compute_student_attendance_summary",
        lambda session, today: calls.append(today) or {"date": today.isoformat(), "total_students": 10},
    )

    first = service.build_student_attendance_summary(db_session, today=target_date)
    second = service.build_student_attendance_summary(db_session, today=target_date)

    assert first == second == {"date": "2026-03-13", "total_students": 10}
    assert calls == [target_date]
