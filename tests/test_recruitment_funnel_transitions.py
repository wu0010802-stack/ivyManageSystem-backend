"""tests/test_recruitment_funnel_transitions.py

驗 transition_visit orchestrator + visited↔deposited dispatch（Task 6 範疇）。
其他 stage dispatch 在 Task 8-10 補。
"""

import os
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.recruitment import RecruitmentVisit, RecruitmentEventLog
from services.recruitment_funnel import (
    transition_visit,
    RecruitmentFunnelError,
)


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


def _make_visit(session, *, has_deposit=False, enrolled=False) -> RecruitmentVisit:
    v = RecruitmentVisit(
        month="115.03",
        child_name="測試幼生",
        has_deposit=has_deposit,
        enrolled=enrolled,
    )
    session.add(v)
    session.flush()
    return v


class TestVisitedDeposited:
    def test_visited_to_deposited(self, session):
        visit = _make_visit(session, has_deposit=False)
        result = transition_visit(
            session,
            visit_id=visit.id,
            to_stage="deposited",
            actor_user_id=99,
        )
        session.flush()

        assert result.from_stage == "visited"
        assert result.to_stage == "deposited"
        assert result.student_id is None
        assert result.event_log_id > 0

        session.refresh(visit)
        assert visit.has_deposit is True

        log = (
            session.query(RecruitmentEventLog)
            .filter_by(recruitment_visit_id=visit.id)
            .one()
        )
        assert log.event_type == "deposit_added"
        assert log.actor_user_id == 99

    def test_deposited_to_visited(self, session):
        visit = _make_visit(session, has_deposit=True)
        result = transition_visit(
            session,
            visit_id=visit.id,
            to_stage="visited",
            actor_user_id=99,
        )
        session.flush()
        session.refresh(visit)
        assert visit.has_deposit is False
        log = (
            session.query(RecruitmentEventLog)
            .filter_by(recruitment_visit_id=visit.id)
            .one()
        )
        assert log.event_type == "deposit_removed"

    def test_same_stage_returns_409_like_error(self, session):
        visit = _make_visit(session, has_deposit=False)
        with pytest.raises(RecruitmentFunnelError) as exc:
            transition_visit(
                session,
                visit_id=visit.id,
                to_stage="visited",
                actor_user_id=99,
            )
        assert exc.value.code == "STAGE_ALREADY"

    def test_visit_not_found(self, session):
        with pytest.raises(RecruitmentFunnelError) as exc:
            transition_visit(
                session,
                visit_id=999999,
                to_stage="deposited",
                actor_user_id=99,
            )
        assert exc.value.code == "VISIT_NOT_FOUND"

    def test_returns_warnings_empty_list(self, session):
        visit = _make_visit(session, has_deposit=False)
        result = transition_visit(
            session,
            visit_id=visit.id,
            to_stage="deposited",
            actor_user_id=99,
        )
        assert result.warnings == []
