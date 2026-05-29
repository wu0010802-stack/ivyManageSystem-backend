"""RecruitmentVisit/IvykidsRecord 加 geocoding_consent_at 欄位後可正常 ORM 操作。"""

from datetime import datetime

from models.recruitment import RecruitmentVisit, RecruitmentIvykidsRecord


def test_recruitment_visit_consent_at_column_exists() -> None:
    """Model class should expose the new column."""
    assert hasattr(RecruitmentVisit, "geocoding_consent_at")
    col = RecruitmentVisit.__table__.c.geocoding_consent_at
    assert col.nullable is True


def test_recruitment_visit_consent_at_set_and_read() -> None:
    """Instantiation accepts the new field."""
    now = datetime(2026, 5, 28, 12, 0, 0)
    v = RecruitmentVisit(
        month="115.05",
        child_name="Test",
        grade="幼兒",
        geocoding_consent_at=now,
    )
    assert v.geocoding_consent_at == now


def test_recruitment_visit_consent_at_defaults_none() -> None:
    v = RecruitmentVisit(month="115.05", child_name="Test", grade="幼兒")
    assert v.geocoding_consent_at is None


def test_recruitment_ivykids_consent_at_column_exists() -> None:
    assert hasattr(RecruitmentIvykidsRecord, "geocoding_consent_at")
    col = RecruitmentIvykidsRecord.__table__.c.geocoding_consent_at
    assert col.nullable is True


def test_recruitment_ivykids_consent_at_defaults_none() -> None:
    r = RecruitmentIvykidsRecord(
        external_id="test-1", month="115.05", child_name="Test"
    )
    assert r.geocoding_consent_at is None
