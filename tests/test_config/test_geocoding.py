import pytest
from config.geocoding import GeocodingSettings


def test_defaults(monkeypatch):
    for var in (
        "GOOGLE_MAPS_API_KEY",
        "GEOCODING_PROVIDER",
        "GEOCODING_USER_AGENT",
        "GEOCODING_CONTACT_EMAIL",
        "GEOCODING_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    s = GeocodingSettings()
    assert s.google_maps_api_key is None
    assert s.provider == "nominatim"
    assert s.user_agent == "ivyManageSystem/1.0"
    assert s.contact_email is None
    assert s.timeout_seconds == 8


def test_env_reads(monkeypatch):
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "AIza...")
    monkeypatch.setenv("GEOCODING_PROVIDER", "google")
    monkeypatch.setenv("GEOCODING_TIMEOUT_SECONDS", "15")
    s = GeocodingSettings()
    assert s.google_maps_api_key == "AIza..."
    assert s.provider == "google"
    assert s.timeout_seconds == 15
