import pytest
from config.line import LineSettings


def test_defaults(monkeypatch):
    for var in (
        "LINE_LOGIN_CHANNEL_ID",
        "LINE_LOGIN_CHANNEL_SECRET",
        "LIFF_ID",
        "LINE_CHANNEL_ACCESS_TOKEN",
        "VITE_LIFF_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    s = LineSettings()
    assert s.login_channel_id is None
    assert s.login_channel_secret is None
    assert s.liff_id is None
    assert s.channel_access_token is None
    assert s.vite_liff_id is None


def test_env_reads(monkeypatch):
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "1234")
    monkeypatch.setenv("LIFF_ID", "1234-abcdef")
    monkeypatch.setenv("VITE_LIFF_ID", "1234-abcdef")
    s = LineSettings()
    assert s.login_channel_id == "1234"
    assert s.liff_id == "1234-abcdef"
    assert s.vite_liff_id == "1234-abcdef"
