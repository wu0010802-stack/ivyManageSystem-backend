def test_consent_enforcement_defaults_false(monkeypatch):
    monkeypatch.delenv("CONSENT_ENFORCEMENT_ENABLED", raising=False)
    from config.consent import ConsentSettings

    assert ConsentSettings().enforcement_enabled is False


def test_consent_enforcement_reads_env(monkeypatch):
    monkeypatch.setenv("CONSENT_ENFORCEMENT_ENABLED", "true")
    from config.consent import ConsentSettings

    assert ConsentSettings().enforcement_enabled is True
