"""RA-HIGH-2：未設可信代理時，偽造 X-Forwarded-For / X-Real-IP 不應被採信。

漏洞：TRUSTED_PROXY_IPS 未明設（預設 "*" → fallback RFC1918 信任）時，
get_client_ip 仍會剝除 XFF 並回傳偽造的最右一跳；攻擊者可任意偽造 per-IP
限流的計數 key（換 IP 繞過 / 嫁禍他人）。X-Real-IP 同理（被無條件採信）。

修法：只有「明設」可信代理（raw env 含 ≥1 個合法 CIDR）時才解析
XFF / X-Real-IP；未明設（空 / "*" / 全無效 token）時忽略轉發標頭，
直接回直連 peer（request.client.host）。

注意：每個 test 用 _reload_request_ip() + reset_for_tests() 清 module 層
memo（_TRUSTED_PROXIES_CACHE），否則讀到上個 test 的快取。
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _reload_request_ip():
    import utils.request_ip as m

    importlib.reload(m)
    return m


def _make_request(xff="", x_real_ip="", client_host="203.0.113.9"):
    headers = {}
    if xff:
        headers["x-forwarded-for"] = xff
    if x_real_ip:
        headers["x-real-ip"] = x_real_ip

    class _Client:
        host = client_host

    class _FakeRequest:
        def __init__(self):
            self.headers = headers
            self.client = _Client()

    return _FakeRequest()


def test_spoofed_xff_ignored_when_no_trusted_proxy(monkeypatch):
    """預設 TRUSTED_PROXY_IPS="*"（未明設）→ 回直連 peer，不採信偽造 XFF。"""
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "*")
    from config import reset_for_tests

    reset_for_tests()
    mod = _reload_request_ip()

    req = _make_request(client_host="203.0.113.9", xff="1.1.1.1, 2.2.2.2")
    assert mod.get_client_ip(req) == "203.0.113.9"


def test_spoofed_x_real_ip_ignored_when_no_trusted_proxy(monkeypatch):
    """未明設可信代理 → X-Real-IP 同樣不可被採信（修法須一併 gate）。"""
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "*")
    from config import reset_for_tests

    reset_for_tests()
    mod = _reload_request_ip()

    req = _make_request(client_host="203.0.113.9", x_real_ip="6.6.6.6")
    assert mod.get_client_ip(req) == "203.0.113.9"


def test_empty_trusted_proxy_also_ignores_xff(monkeypatch):
    """TRUSTED_PROXY_IPS 空字串（未設）→ 同樣忽略 XFF 回直連 peer。"""
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "")
    from config import reset_for_tests

    reset_for_tests()
    mod = _reload_request_ip()

    req = _make_request(client_host="203.0.113.9", xff="1.1.1.1")
    assert mod.get_client_ip(req) == "203.0.113.9"


def test_explicit_trusted_proxy_still_parses_xff(monkeypatch):
    """明設合法 CIDR → 仍解析 XFF，回鏈中第一個非信任 IP（行為不變）。"""
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.0/8")
    from config import reset_for_tests

    reset_for_tests()
    mod = _reload_request_ip()

    req = _make_request(client_host="10.0.0.1", xff="203.0.113.5, 10.0.0.1")
    assert mod.get_client_ip(req) == "203.0.113.5"


def test_explicit_trusted_proxy_still_honors_x_real_ip(monkeypatch):
    """明設合法 CIDR + 無 XFF → 仍採信 X-Real-IP（行為不變）。"""
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.0/8")
    from config import reset_for_tests

    reset_for_tests()
    mod = _reload_request_ip()

    req = _make_request(client_host="10.0.0.1", x_real_ip="8.8.8.8")
    assert mod.get_client_ip(req) == "8.8.8.8"


def test_no_forwarded_headers_returns_peer(monkeypatch):
    """無轉發標頭時，不論是否明設可信代理，皆回直連 peer。"""
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "*")
    from config import reset_for_tests

    reset_for_tests()
    mod = _reload_request_ip()

    req = _make_request(client_host="5.6.7.8")
    assert mod.get_client_ip(req) == "5.6.7.8"
