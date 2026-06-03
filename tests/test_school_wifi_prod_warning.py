"""prod 未設 SCHOOL_WIFI_IPS 時教師 WiFi 閘 fail-open，啟動須告警（稽核 2026-06-03 P2-e）。

_is_school_wifi 在白名單空時 return True（fail-open），且原本無 prod 可見性 —— 若 prod
漏設 SCHOOL_WIFI_IPS，教師端「須連學校 WiFi 才能登入」這條唯一網路層存取控制 silently
失效，任何取得教師帳密者可從任意公網 IP 登入。

修法：不改 fail-open（避免鎖死未設定環境），但加啟動告警 warn_if_school_wifi_gate_disabled
讓 prod 下的閘停用變可見（main.py 啟動呼叫）。
"""

import ipaddress
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.auth as auth
from api.auth import warn_if_school_wifi_gate_disabled


def test_warns_when_prod_and_wifi_unset(caplog, monkeypatch):
    monkeypatch.setattr(auth, "_get_school_wifi_networks", lambda: [])
    with caplog.at_level(logging.WARNING):
        warn_if_school_wifi_gate_disabled(is_production=True)
    assert any("SCHOOL_WIFI_IPS" in r.message for r in caplog.records), caplog.text


def test_no_warn_when_wifi_configured(caplog, monkeypatch):
    monkeypatch.setattr(
        auth,
        "_get_school_wifi_networks",
        lambda: [ipaddress.ip_network("10.0.0.0/8")],
    )
    with caplog.at_level(logging.WARNING):
        warn_if_school_wifi_gate_disabled(is_production=True)
    assert not any("SCHOOL_WIFI_IPS" in r.message for r in caplog.records)


def test_no_warn_in_dev_even_if_unset(caplog, monkeypatch):
    """dev / 非 prod 環境未設不告警（本機開發允許 fail-open）。"""
    monkeypatch.setattr(auth, "_get_school_wifi_networks", lambda: [])
    with caplog.at_level(logging.WARNING):
        warn_if_school_wifi_gate_disabled(is_production=False)
    assert not any("SCHOOL_WIFI_IPS" in r.message for r in caplog.records)
