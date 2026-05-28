"""POST/PUT /api/recruitment/records 接 consent → consent_at 寫入

直接呼叫 endpoint function 繞過 auth（同 import_recruitment_records test pattern）。
"""

import pytest

from utils.taipei_time import now_taipei_naive
from tests.test_recruitment_api import (  # noqa: F401
    recruitment_session_factory,
)
from api.recruitment.records import (
    create_recruitment_record,
    update_recruitment_record,
)
from api.recruitment.shared import RecruitmentVisitCreate, RecruitmentVisitUpdate
from models.recruitment import RecruitmentVisit


def _fetch_visit(recruitment_session_factory, visit_id):
    with recruitment_session_factory() as s:
        return s.query(RecruitmentVisit).filter_by(id=visit_id).one()


def test_post_records_with_consent_true_sets_consent_at(
    recruitment_session_factory,
) -> None:
    payload = RecruitmentVisitCreate(
        month="115.05",
        child_name="TC",
        grade="幼兒",
        geocoding_consent=True,
    )
    result = create_recruitment_record(payload, _=None)
    visit_id = result["id"]

    v = _fetch_visit(recruitment_session_factory, visit_id)
    assert v.geocoding_consent_at is not None
    delta = abs((now_taipei_naive() - v.geocoding_consent_at).total_seconds())
    assert delta < 60


def test_post_records_with_consent_false_sets_null(recruitment_session_factory) -> None:
    payload = RecruitmentVisitCreate(
        month="115.05",
        child_name="TC",
        grade="幼兒",
        geocoding_consent=False,
    )
    result = create_recruitment_record(payload, _=None)
    visit_id = result["id"]

    v = _fetch_visit(recruitment_session_factory, visit_id)
    assert v.geocoding_consent_at is None


def test_post_records_default_consent_false(recruitment_session_factory) -> None:
    """payload 不帶 consent → 預設 False → consent_at IS NULL"""
    payload = RecruitmentVisitCreate(
        month="115.05",
        child_name="TC",
        grade="幼兒",
    )
    result = create_recruitment_record(payload, _=None)
    visit_id = result["id"]

    v = _fetch_visit(recruitment_session_factory, visit_id)
    assert v.geocoding_consent_at is None


def test_put_records_consent_true_writes_now(recruitment_session_factory) -> None:
    create_payload = RecruitmentVisitCreate(
        month="115.05",
        child_name="TC2",
        grade="幼兒",
        geocoding_consent=False,
    )
    create = create_recruitment_record(create_payload, _=None)
    visit_id = create["id"]

    upd_payload = RecruitmentVisitUpdate(geocoding_consent=True)
    update_recruitment_record(visit_id, upd_payload, _=None)

    v = _fetch_visit(recruitment_session_factory, visit_id)
    assert v.geocoding_consent_at is not None


def test_put_records_consent_false_clears(recruitment_session_factory) -> None:
    create_payload = RecruitmentVisitCreate(
        month="115.05",
        child_name="TC3",
        grade="幼兒",
        geocoding_consent=True,
    )
    create = create_recruitment_record(create_payload, _=None)
    visit_id = create["id"]

    upd_payload = RecruitmentVisitUpdate(geocoding_consent=False)
    update_recruitment_record(visit_id, upd_payload, _=None)

    v = _fetch_visit(recruitment_session_factory, visit_id)
    assert v.geocoding_consent_at is None


def test_put_records_consent_none_preserves(recruitment_session_factory) -> None:
    """Update 不帶 consent field → 保留既有"""
    create_payload = RecruitmentVisitCreate(
        month="115.05",
        child_name="TC4",
        grade="幼兒",
        geocoding_consent=True,
    )
    create = create_recruitment_record(create_payload, _=None)
    visit_id = create["id"]

    # update other field only
    upd_payload = RecruitmentVisitUpdate(child_name="TC4-rename")
    update_recruitment_record(visit_id, upd_payload, _=None)

    v = _fetch_visit(recruitment_session_factory, visit_id)
    assert v.geocoding_consent_at is not None  # preserved
