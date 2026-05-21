import pytest
from config.network import NetworkSettings


def test_defaults(monkeypatch):
    for var in (
        "CORS_ORIGINS",
        "ALLOWED_HOSTS",
        "TRUSTED_PROXY_IPS",
        "CSP_SCRIPT_HASHES",
        "COOKIE_SAMESITE",
        "SCHOOL_WIFI_IPS",
        "RATE_LIMIT_BACKEND",
    ):
        monkeypatch.delenv(var, raising=False)
    s = NetworkSettings()
    assert s.cors_origins == []
    assert s.allowed_hosts == []
    assert s.trusted_proxy_ips == "*"
    assert s.csp_script_hashes == []
    assert (
        s.cookie_samesite == "strict"
    )  # 對齊 utils/cookie.py 原始安全預設（CSRF 最強防護）
    assert s.school_wifi_ips == []
    assert s.rate_limit_backend == "memory"


def test_csv_parsing(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173,https://example.com")
    monkeypatch.setenv("ALLOWED_HOSTS", " a.com , b.com ")
    monkeypatch.setenv("SCHOOL_WIFI_IPS", "192.168.1.0/24,10.0.0.0/8")
    s = NetworkSettings()
    assert s.cors_origins == ["http://localhost:5173", "https://example.com"]
    assert s.allowed_hosts == ["a.com", "b.com"]
    assert s.school_wifi_ips == ["192.168.1.0/24", "10.0.0.0/8"]


def test_cookie_samesite_literal(monkeypatch):
    monkeypatch.setenv("COOKIE_SAMESITE", "strict")
    s = NetworkSettings()
    assert s.cookie_samesite == "strict"
