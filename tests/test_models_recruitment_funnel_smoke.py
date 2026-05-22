"""tests/test_models_recruitment_funnel_smoke.py — Smoke test for recruitment funnel models."""

from models.academic_term import AcademicTerm
from models.recruitment import RecruitmentEventLog


def test_academic_term_columns():
    cols = {c.name for c in AcademicTerm.__table__.columns}
    assert {"id", "school_year", "semester", "start_date", "end_date"} <= cols


def test_recruitment_event_log_columns():
    cols = {c.name for c in RecruitmentEventLog.__table__.columns}
    assert {
        "id",
        "recruitment_visit_id",
        "event_type",
        "from_stage",
        "to_stage",
        "student_id",
        "reason",
        "actor_user_id",
        "metadata_json",
        "created_at",
    } <= cols
