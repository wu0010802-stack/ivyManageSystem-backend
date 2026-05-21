import pytest
from config.recruitment import RecruitmentSettings

_ALL_VARS = (
    "IVYKIDS_USERNAME",
    "IVYKIDS_PASSWORD",
    "IVYKIDS_LOGIN_URL",
    "IVYKIDS_DATA_URL",
    "IVYKIDS_SYNC_ENABLED",
    "IVYKIDS_SYNC_INTERVAL_MINUTES",
    "RECRUITMENT_CAMPUS_NAME",
    "RECRUITMENT_CAMPUS_ADDRESS",
    "RECRUITMENT_CAMPUS_LAT",
    "RECRUITMENT_CAMPUS_LNG",
    "RECRUITMENT_CAMPUS_TRAVEL_MODE",
    "TGOS_APP_ID",
    "TGOS_API_KEY",
    "RECRUITMENT_MARKET_TIMEOUT_SECONDS",
)


def test_defaults(monkeypatch):
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)
    s = RecruitmentSettings()
    assert s.ivykids_username is None
    assert s.ivykids_password is None
    assert s.ivykids_login_url == "https://www.ivykids.tw/manage/"
    assert s.ivykids_data_url == "https://www.ivykids.tw/manage/make_an_appointment/"
    assert s.ivykids_sync_enabled is False
    assert s.ivykids_sync_interval_minutes == 10
    assert s.campus_name == "本園"  # 對齊 services 原 default
    assert s.campus_lat is None
    assert s.campus_lng is None
    assert s.campus_travel_mode == "driving"
    assert s.market_timeout_seconds == 12  # 對齊 services 原 default


def test_lat_lng_float(monkeypatch):
    monkeypatch.setenv("RECRUITMENT_CAMPUS_LAT", "25.0330")
    monkeypatch.setenv("RECRUITMENT_CAMPUS_LNG", "121.5654")
    s = RecruitmentSettings()
    assert s.campus_lat == 25.0330
    assert s.campus_lng == 121.5654
