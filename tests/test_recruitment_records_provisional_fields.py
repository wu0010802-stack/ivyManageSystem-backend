"""RecruitmentRecordOut 應曝露 provisional 座位欄位（班級頁準新生 / 訪視記錄保留座位顯示用）。

涵蓋：
- schema 欄位存在
- model_validate(orm) 帶出值（單筆 create/update 路徑，from_attributes）
- _to_dict 帶出值（列表路徑，手動組 dict）
"""

from api.recruitment.shared import _to_dict
from models.recruitment import RecruitmentVisit
from schemas.recruitment_records import RecruitmentRecordOut


def _make_visit(**kw) -> RecruitmentVisit:
    base = dict(
        id=1,
        month="115.03",
        child_name="測試童",
        has_deposit=True,
        provisional_grade_id=None,
        target_school_year=None,
        target_semester=None,
    )
    base.update(kw)
    return RecruitmentVisit(**base)


def test_record_out_has_provisional_fields():
    fields = RecruitmentRecordOut.model_fields
    assert "provisional_grade_id" in fields
    assert "target_school_year" in fields
    assert "target_semester" in fields


def test_model_validate_populates_provisional_from_orm():
    out_none = RecruitmentRecordOut.model_validate(_make_visit())
    assert out_none.provisional_grade_id is None
    assert out_none.target_school_year is None

    out_set = RecruitmentRecordOut.model_validate(
        _make_visit(provisional_grade_id=7, target_school_year=115, target_semester=1)
    )
    assert out_set.provisional_grade_id == 7
    assert out_set.target_school_year == 115
    assert out_set.target_semester == 1


def test_to_dict_includes_provisional_fields():
    d = _to_dict(
        _make_visit(provisional_grade_id=7, target_school_year=115, target_semester=2)
    )
    assert d["provisional_grade_id"] == 7
    assert d["target_school_year"] == 115
    assert d["target_semester"] == 2
