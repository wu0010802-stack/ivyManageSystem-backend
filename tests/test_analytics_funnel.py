"""funnel_service tests"""

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.base import Base
from models.recruitment import RecruitmentVisit
from models.activity import ParentInquiry
from services.analytics.funnel_service import (
    count_visit_side_stages,
    summarize_no_deposit_reasons,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _add_visit(
    session,
    *,
    month,
    has_deposit=False,
    enrolled=False,
    source="walk_in",
    grade="小班",
    no_deposit_reason=None,
    child_name="幼生A",
):
    v = RecruitmentVisit(
        month=month,
        child_name=child_name,
        source=source,
        grade=grade,
        has_deposit=has_deposit,
        enrolled=enrolled,
        no_deposit_reason=no_deposit_reason,
    )
    session.add(v)
    session.commit()
    return v


def _add_inquiry(session, *, created_at):
    # ParentInquiry 欄位：name, phone, question, is_read
    inq = ParentInquiry(
        name="家長A",
        phone="0900",
        question="詢問",
        created_at=created_at,
        is_read=False,
    )
    session.add(inq)
    session.commit()
    return inq


def test_visit_count_basic(session):
    # 3 visits in 2026-03
    _add_visit(session, month="115.03", has_deposit=False, enrolled=False)
    _add_visit(session, month="115.03", has_deposit=True, enrolled=False)
    _add_visit(session, month="115.03", has_deposit=True, enrolled=True)
    # 1 inquiry in 2026-03
    _add_inquiry(session, created_at=datetime(2026, 3, 5, 10, 0))

    result = count_visit_side_stages(
        session, start_date=date(2026, 3, 1), end_date=date(2026, 3, 31)
    )
    # lead = 3 visits + 1 inquiry = 4
    assert result["lead"] == 4
    assert result["deposit"] == 2
    assert result["enrolled"] == 1


def test_visit_count_filters_by_grade_and_source(session):
    _add_visit(
        session,
        month="115.03",
        grade="小班",
        source="walk_in",
        has_deposit=True,
        enrolled=True,
    )
    _add_visit(
        session,
        month="115.03",
        grade="中班",
        source="walk_in",
        has_deposit=True,
        enrolled=True,
    )
    _add_visit(
        session,
        month="115.03",
        grade="小班",
        source="referral",
        has_deposit=True,
        enrolled=True,
    )

    r = count_visit_side_stages(
        session,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
        grade_filter="小班",
    )
    assert r["enrolled"] == 2

    r2 = count_visit_side_stages(
        session,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
        grade_filter="小班",
        source_filter="walk_in",
    )
    assert r2["enrolled"] == 1


def test_visit_invalid_month_skipped(session):
    _add_visit(session, month="bad-month", has_deposit=True, enrolled=True)
    _add_visit(session, month="115.03", has_deposit=True, enrolled=True)

    r = count_visit_side_stages(
        session, start_date=date(2026, 3, 1), end_date=date(2026, 3, 31)
    )
    assert r["enrolled"] == 1  # 'bad-month' 略過


def test_no_deposit_reasons(session):
    _add_visit(session, month="115.03", has_deposit=False, no_deposit_reason="考慮中")
    _add_visit(session, month="115.03", has_deposit=False, no_deposit_reason="考慮中")
    _add_visit(session, month="115.03", has_deposit=False, no_deposit_reason="選擇他校")
    _add_visit(session, month="115.03", has_deposit=True, no_deposit_reason=None)
    _add_visit(
        session, month="115.03", has_deposit=False, no_deposit_reason=None
    )  # 沒填原因，不計入

    reasons = summarize_no_deposit_reasons(
        session, start_date=date(2026, 3, 1), end_date=date(2026, 3, 31)
    )
    by_reason = {r["reason"]: r["count"] for r in reasons}
    assert by_reason == {"考慮中": 2, "選擇他校": 1}
