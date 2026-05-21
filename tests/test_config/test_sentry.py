import pytest
from config.sentry import SentrySettings


def test_defaults(monkeypatch):
    for var in (
        "SENTRY_DSN",
        "SENTRY_ENVIRONMENT",
        "SENTRY_RELEASE",
        "SENTRY_TRACES_SAMPLE_RATE",
    ):
        monkeypatch.delenv(var, raising=False)
    s = SentrySettings()
    assert s.dsn is None
    assert s.environment == "production"
    assert s.release is None
    assert s.traces_sample_rate == 0.1
    assert s.enabled is False


def test_enabled_when_dsn_set(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "https://abc@sentry.io/1")
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.5")
    s = SentrySettings()
    assert s.dsn == "https://abc@sentry.io/1"
    assert s.enabled is True
    assert s.traces_sample_rate == 0.5
