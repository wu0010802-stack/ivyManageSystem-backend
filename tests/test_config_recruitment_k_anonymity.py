"""RECRUITMENT_K_ANONYMITY_THRESHOLD config 驗證。"""

import pytest


def test_k_anonymity_threshold_default_is_5(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECRUITMENT_K_ANONYMITY_THRESHOLD", raising=False)
    from config.recruitment import RecruitmentSettings

    s = RecruitmentSettings()
    assert s.k_anonymity_threshold == 5


def test_k_anonymity_threshold_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECRUITMENT_K_ANONYMITY_THRESHOLD", "3")
    from config.recruitment import RecruitmentSettings

    s = RecruitmentSettings()
    assert s.k_anonymity_threshold == 3


def test_k_anonymity_threshold_clamp_low(monkeypatch: pytest.MonkeyPatch) -> None:
    """clamp [2, 10] — K=1 dangerous → 升到 2"""
    monkeypatch.setenv("RECRUITMENT_K_ANONYMITY_THRESHOLD", "1")
    from config.recruitment import RecruitmentSettings

    s = RecruitmentSettings()
    assert s.k_anonymity_threshold == 2


def test_k_anonymity_threshold_clamp_high(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECRUITMENT_K_ANONYMITY_THRESHOLD", "100")
    from config.recruitment import RecruitmentSettings

    s = RecruitmentSettings()
    assert s.k_anonymity_threshold == 10
