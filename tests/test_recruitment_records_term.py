"""訪視記錄建立/更新 — 入學學期欄位 + create 缺值預設當前學期。

與 test_recruitment_consent_endpoint.py 同模式：直接呼叫 endpoint function
（`_=None` 繞過 auth），使用 recruitment_session_factory fixture。
"""

import pytest

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


def test_create_record_persists_target_term(recruitment_session_factory):
    """明確傳入 target_school_year/target_semester 時，回傳值與 DB 均持久化。"""
    payload = RecruitmentVisitCreate(
        month="114.09",
        child_name="測試童甲",
        target_school_year=115,
        target_semester=1,
    )
    result = create_recruitment_record(payload, _=None)

    assert result["target_school_year"] == 115
    assert result["target_semester"] == 1

    v = _fetch_visit(recruitment_session_factory, result["id"])
    assert v.target_school_year == 115
    assert v.target_semester == 1


def test_create_record_defaults_to_current_term_when_missing(
    recruitment_session_factory,
):
    """未傳 target_school_year/target_semester 時，handler 填入當前學期。"""
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()

    payload = RecruitmentVisitCreate(month="114.09", child_name="測試童乙")
    result = create_recruitment_record(payload, _=None)

    assert result["target_school_year"] == sy
    assert result["target_semester"] == sem

    v = _fetch_visit(recruitment_session_factory, result["id"])
    assert v.target_school_year == sy
    assert v.target_semester == sem


def test_update_record_changes_target_term(recruitment_session_factory):
    """PUT 更新可修改 target_school_year/target_semester。"""
    create_payload = RecruitmentVisitCreate(
        month="114.09",
        child_name="測試童丙",
        target_school_year=114,
        target_semester=1,
    )
    created = create_recruitment_record(create_payload, _=None)
    rid = created["id"]

    update_payload = RecruitmentVisitUpdate(
        target_school_year=115,
        target_semester=2,
    )
    result = update_recruitment_record(rid, update_payload, _=None)

    assert result["target_school_year"] == 115
    assert result["target_semester"] == 2

    v = _fetch_visit(recruitment_session_factory, rid)
    assert v.target_school_year == 115
    assert v.target_semester == 2


def test_create_record_rejects_bad_semester(recruitment_session_factory):
    """target_semester=3 應觸發 Pydantic 422 驗證錯誤。"""
    with pytest.raises(Exception):
        RecruitmentVisitCreate(
            month="114.09",
            child_name="測試童丁",
            target_school_year=115,
            target_semester=3,
        )
