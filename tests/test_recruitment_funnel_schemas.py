"""驗 Pydantic schemas 的基本 validation。"""

import pytest
from datetime import date
from pydantic import ValidationError

from schemas.academic_term import AcademicTermIn, AcademicTermOut
from schemas.recruitment_funnel import (
    FunnelCard,
    FunnelBoardOut,
    FunnelSummary,
    TransitionIn,
    TransitionOut,
    TimelineOut,
    TimelineEvent,
)


def test_academic_term_in_rejects_inverted_dates():
    with pytest.raises(ValidationError) as exc:
        AcademicTermIn(
            school_year=115,
            semester=1,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 8, 1),
        )
    assert "end_date" in str(exc.value)


def test_academic_term_in_rejects_bad_semester():
    with pytest.raises(ValidationError):
        AcademicTermIn(
            school_year=115,
            semester=3,
            start_date=date(2026, 8, 1),
            end_date=date(2027, 1, 31),
        )


def test_academic_term_in_accepts_valid():
    t = AcademicTermIn(
        school_year=115,
        semester=1,
        start_date=date(2026, 8, 30),
        end_date=date(2027, 1, 31),
    )
    assert t.school_year == 115


def test_transition_in_requires_to_stage():
    with pytest.raises(ValidationError):
        TransitionIn()


def test_transition_in_validates_to_stage():
    with pytest.raises(ValidationError):
        TransitionIn(to_stage="invalid")


def test_transition_out_defaults_warnings_empty():
    o = TransitionOut(
        visit_id=1,
        from_stage="visited",
        to_stage="deposited",
        student_id=None,
        event_log_id=42,
    )
    assert o.warnings == []
