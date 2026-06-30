# tests/test_kiosk_ip_guard.py
import pytest
from fastapi import HTTPException
from utils.kiosk_guard import assert_kiosk_ip_allowed


class _Req:
    def __init__(self, host):
        self.client = type("C", (), {"host": host})()
        self.headers = {}


def test_empty_whitelist_is_fail_closed(monkeypatch):
    monkeypatch.setattr("utils.kiosk_guard.get_client_ip", lambda r: "203.0.113.9")
    monkeypatch.setattr(
        "config.settings.network.attendance_kiosk_allowed_ips", [], raising=False
    )
    with pytest.raises(HTTPException) as ei:
        assert_kiosk_ip_allowed(_Req("203.0.113.9"))
    assert ei.value.status_code == 403


def test_ip_in_whitelist_passes(monkeypatch):
    monkeypatch.setattr("utils.kiosk_guard.get_client_ip", lambda r: "203.0.113.10")
    monkeypatch.setattr(
        "config.settings.network.attendance_kiosk_allowed_ips",
        ["203.0.113.0/24"],
        raising=False,
    )
    assert assert_kiosk_ip_allowed(_Req("203.0.113.10")) is None


def test_ip_not_in_whitelist_403(monkeypatch):
    monkeypatch.setattr("utils.kiosk_guard.get_client_ip", lambda r: "198.51.100.7")
    monkeypatch.setattr(
        "config.settings.network.attendance_kiosk_allowed_ips",
        ["203.0.113.0/24"],
        raising=False,
    )
    with pytest.raises(HTTPException) as ei:
        assert_kiosk_ip_allowed(_Req("198.51.100.7"))
    assert ei.value.status_code == 403
