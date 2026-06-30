from config.network import NetworkSettings


def test_kiosk_allowed_ips_default_empty():
    s = NetworkSettings()
    assert s.attendance_kiosk_allowed_ips == []


def test_kiosk_allowed_ips_parses_csv(monkeypatch):
    monkeypatch.setenv("ATTENDANCE_KIOSK_ALLOWED_IPS", "203.0.113.10/32, 10.0.0.5")
    s = NetworkSettings()
    assert s.attendance_kiosk_allowed_ips == ["203.0.113.10/32", "10.0.0.5"]
