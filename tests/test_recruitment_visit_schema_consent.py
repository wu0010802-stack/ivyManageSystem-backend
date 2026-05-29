"""RecruitmentVisitCreate/Update 接 geocoding_consent boolean。"""

from api.recruitment.shared import RecruitmentVisitCreate, RecruitmentVisitUpdate


def test_visit_create_default_consent_false() -> None:
    """業主決議 explicit attestation — 預設不勾"""
    payload = RecruitmentVisitCreate(
        month="115.05", child_name="Test", grade="幼兒"
    )
    assert payload.geocoding_consent is False


def test_visit_create_consent_true() -> None:
    payload = RecruitmentVisitCreate(
        month="115.05", child_name="Test", grade="幼兒",
        geocoding_consent=True,
    )
    assert payload.geocoding_consent is True


def test_visit_update_default_consent_none() -> None:
    """Update path: None = 不修改 consent；True/False = 修改"""
    payload = RecruitmentVisitUpdate()
    assert payload.geocoding_consent is None


def test_visit_update_consent_true_or_false() -> None:
    p1 = RecruitmentVisitUpdate(geocoding_consent=True)
    assert p1.geocoding_consent is True

    p2 = RecruitmentVisitUpdate(geocoding_consent=False)
    assert p2.geocoding_consent is False
