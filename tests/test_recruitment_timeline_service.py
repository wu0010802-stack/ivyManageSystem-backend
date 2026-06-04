"""build_visit_timeline：union recruitment_event_log + student_change_logs，按時間排序。"""

import os
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.recruitment import RecruitmentVisit, RecruitmentEventLog
from services.recruitment_timeline import build_visit_timeline, TimelineNotFound


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _seed_visit(session) -> int:
    v = RecruitmentVisit(month="115.03", child_name="時光童", has_deposit=False)
    session.add(v)
    session.flush()
    return v.id


def test_not_found_raises(session):
    with pytest.raises(TimelineNotFound):
        build_visit_timeline(session, visit_id=999999)


def test_visit_with_only_recruitment_events(session):
    vid = _seed_visit(session)
    session.add(
        RecruitmentEventLog(
            recruitment_visit_id=vid,
            event_type="created",
            from_stage=None,
            to_stage="visited",
            actor_user_id=1,
            reason=None,
            created_at=datetime(2026, 3, 1, 9, 0),
        )
    )
    session.flush()
    events = build_visit_timeline(session, visit_id=vid)
    assert len(events) == 1
    assert events[0].source == "recruitment"
    assert events[0].event_type == "created"


def test_events_sorted_by_time(session):
    vid = _seed_visit(session)
    session.add_all(
        [
            RecruitmentEventLog(
                recruitment_visit_id=vid,
                event_type="deposit_added",
                from_stage="visited",
                to_stage="deposited",
                created_at=datetime(2026, 3, 10, 9, 0),
            ),
            RecruitmentEventLog(
                recruitment_visit_id=vid,
                event_type="created",
                from_stage=None,
                to_stage="visited",
                created_at=datetime(2026, 3, 1, 9, 0),
            ),
        ]
    )
    session.flush()
    events = build_visit_timeline(session, visit_id=vid)
    assert [e.event_type for e in events] == ["created", "deposit_added"]


def test_timeline_endpoint_registered_under_api_recruitment():
    """守衛：歷程端點掛在正確的 /api/recruitment 前綴（不重蹈 funnel 缺 /api 的覆轍）。"""
    from main import app

    paths = [r.path for r in app.routes]
    assert "/api/recruitment/visits/{visit_id}/timeline" in paths
