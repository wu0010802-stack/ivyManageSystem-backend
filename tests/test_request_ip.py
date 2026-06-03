"""tests/test_request_ip.py — utils/request_ip.py 測試

涵蓋：
1. A10-1 memoize：TRUSTED_PROXY_IPS="*" 多次呼叫 _parse_trusted_proxies，
   warning 只發一次（memoize 防 prod log 洗版）
2. 合法 CIDR 解析正確，無 fallback warning
3. get_client_ip XFF 剝除邏輯（正確性不受 memoize 影響）
"""

import importlib
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── helpers ───────────────────────────────────────────────────────────────────


def _reload_request_ip():
    """重載 utils.request_ip 以清除 module 層 memo 狀態。"""
    import utils.request_ip as m

    importlib.reload(m)
    return m


def _make_request(xff: str = "", x_real_ip: str = "", client_host: str = "127.0.0.1"):
    """組一個最小 fake Request 物件。"""
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


# ── A10-1 memoize：warning 只應發一次 ─────────────────────────────────────────


class TestParseTrustedProxiesMemoize:
    """_parse_trusted_proxies memoize 行為：多次呼叫只 warn 一次。"""

    def test_wildcard_warns_only_once_across_multiple_calls(self, monkeypatch, caplog):
        """TRUSTED_PROXY_IPS='*' 呼叫 5 次，warning 計數應 == 1（不是 5）。"""
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "*")
        from config import reset_for_tests

        reset_for_tests()

        mod = _reload_request_ip()

        with caplog.at_level(logging.WARNING, logger="utils.request_ip"):
            for _ in range(5):
                mod._parse_trusted_proxies()

        fallback_warns = [
            r
            for r in caplog.records
            if "TRUSTED_PROXY_IPS" in r.message and r.levelno == logging.WARNING
        ]
        assert len(fallback_warns) == 1, (
            f"期望 warning 只發一次，但實際發了 {len(fallback_warns)} 次。"
            "memoize 未正確實作或 cache 未生效。"
        )

    def test_wildcard_returns_rfc1918_defaults(self, monkeypatch):
        """TRUSTED_PROXY_IPS='*' 應 fallback 成 RFC1918。"""
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "*")
        from config import reset_for_tests

        reset_for_tests()

        mod = _reload_request_ip()
        result = mod._parse_trusted_proxies()

        import ipaddress

        # 確認含 10.0.0.0/8
        assert any(
            str(n) == "10.0.0.0/8" for n in result
        ), f"期望包含 RFC1918 10.0.0.0/8，實際：{result}"
        # 確認不含 "*" 本身（"*" 不是合法 ip_network）
        assert all(
            isinstance(n, (ipaddress.IPv4Network, ipaddress.IPv6Network))
            for n in result
        )

    def test_all_invalid_tokens_warns_only_once(self, monkeypatch, caplog):
        """全部 token 無效（非 CIDR）時，warning 也只發一次。"""
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "invalid,garbage,!!!")
        from config import reset_for_tests

        reset_for_tests()

        mod = _reload_request_ip()

        with caplog.at_level(logging.WARNING, logger="utils.request_ip"):
            for _ in range(3):
                mod._parse_trusted_proxies()

        warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warns) >= 1, "應有 warning"
        # 每個 unique raw 只應整體 warn 一次（不論幾個 token）
        # 呼叫 3 次但只應 warn 1 組（不是 3 組）
        # 實際 warn 數 ≤ token 數（無效 token 個別 warn）× 1 輪
        first_call_warn_count = len(warns)
        caplog.clear()

        # 再呼叫 3 次，不應再有新 warning（memoize 命中）
        with caplog.at_level(logging.WARNING, logger="utils.request_ip"):
            for _ in range(3):
                mod._parse_trusted_proxies()

        second_call_warns = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert (
            len(second_call_warns) == 0
        ), f"memoize 命中後不應再發 warning，但第二輪又發了 {len(second_call_warns)} 條。"

    def test_warning_message_contains_prod_guidance(self, monkeypatch, caplog):
        """warning 訊息應明確提示 prod 應設定 TRUSTED_PROXY_IPS，否則限流可能被繞過。"""
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "*")
        from config import reset_for_tests

        reset_for_tests()

        mod = _reload_request_ip()

        with caplog.at_level(logging.WARNING, logger="utils.request_ip"):
            mod._parse_trusted_proxies()

        full_text = " ".join(
            r.message for r in caplog.records if r.levelno == logging.WARNING
        )
        # 應包含對 prod 的指引（含 TRUSTED_PROXY_IPS 或限流提示）
        assert (
            "prod" in full_text.lower() or "TRUSTED_PROXY_IPS" in full_text
        ), f"warning 訊息未包含 prod 指引。實際訊息：{full_text!r}"


# ── 合法 CIDR 解析 ────────────────────────────────────────────────────────────


class TestParseTrustedProxiesValid:
    """合法 CIDR 時，解析正確、無 fallback warning。"""

    def test_valid_cidr_parsed_correctly(self, monkeypatch, caplog):
        """合法 CIDR 設定 → 正確解析，不落入 RFC1918 fallback。"""
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.0/8,1.2.3.0/24")
        from config import reset_for_tests

        reset_for_tests()

        mod = _reload_request_ip()

        with caplog.at_level(logging.WARNING, logger="utils.request_ip"):
            result = mod._parse_trusted_proxies()

        import ipaddress

        cidrs = [str(n) for n in result]
        assert "10.0.0.0/8" in cidrs
        assert "1.2.3.0/24" in cidrs
        assert len(result) == 2

        # 不應有 fallback warning
        fallback_warns = [
            r
            for r in caplog.records
            if "prod" in r.message.lower()
            or "RFC1918" in r.message
            or "fallback" in r.message.lower()
        ]
        assert (
            len(fallback_warns) == 0
        ), f"合法 CIDR 不應有 fallback warning，但有：{[r.message for r in fallback_warns]}"

    def test_env_change_triggers_reparse(self, monkeypatch):
        """env 改變後（不同 raw），應重新解析（memoize 以 raw 為 key，不永久快取）。"""
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "192.168.1.0/24")
        from config import reset_for_tests

        reset_for_tests()
        mod = _reload_request_ip()

        result1 = mod._parse_trusted_proxies()
        cidrs1 = [str(n) for n in result1]
        assert "192.168.1.0/24" in cidrs1

        # 改 env，重設 settings cache，重載模組
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "172.16.0.0/12")
        reset_for_tests()
        mod2 = _reload_request_ip()

        result2 = mod2._parse_trusted_proxies()
        cidrs2 = [str(n) for n in result2]
        assert "172.16.0.0/12" in cidrs2, "env 改變後應重新解析，但仍拿到舊結果"


# ── get_client_ip XFF 剝除邏輯 ────────────────────────────────────────────────


class TestGetClientIp:
    """get_client_ip 正確性（memoize 不影響 XFF 剝除邏輯）。"""

    def test_xff_not_trusted_returns_xff_ip(self, monkeypatch):
        """XFF 中有非信任 IP → 回傳該 IP。"""
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.0/8")
        from config import reset_for_tests

        reset_for_tests()
        mod = _reload_request_ip()

        req = _make_request(xff="203.0.113.5, 10.0.0.1")
        result = mod.get_client_ip(req)
        assert result == "203.0.113.5"

    def test_xff_all_trusted_returns_first(self, monkeypatch):
        """XFF chain 全部是 trusted → 回傳最左邊（chain[0]）。"""
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.0/8")
        from config import reset_for_tests

        reset_for_tests()
        mod = _reload_request_ip()

        req = _make_request(xff="10.0.0.2, 10.0.0.1")
        result = mod.get_client_ip(req)
        assert result == "10.0.0.2"

    def test_no_xff_falls_back_to_client_host(self, monkeypatch):
        """無 XFF header → 回傳 request.client.host。"""
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.0/8")
        from config import reset_for_tests

        reset_for_tests()
        mod = _reload_request_ip()

        req = _make_request(client_host="5.6.7.8")
        result = mod.get_client_ip(req)
        assert result == "5.6.7.8"

    def test_x_real_ip_used_when_no_xff(self, monkeypatch):
        """無 XFF 但有 X-Real-IP → 回傳 X-Real-IP。"""
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.0/8")
        from config import reset_for_tests

        reset_for_tests()
        mod = _reload_request_ip()

        req = _make_request(x_real_ip="8.8.8.8", client_host="10.0.0.1")
        result = mod.get_client_ip(req)
        assert result == "8.8.8.8"

    def test_x_real_ip_ignored_from_untrusted_peer(self, monkeypatch):
        """X-Real-IP 來自非 trusted peer（直連攻擊者）→ 忽略並回傳真實 peer。

        防：送無 XFF、帶偽造 X-Real-IP（每次不同）的請求，讓每次落不同 rate-limit
        bucket 繞過 per-IP 限流 / 污染 audit IP。X-Real-IP 應只在 peer 為 trusted proxy
        （nginx 設此 header）時採信。
        """
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.0/8")
        from config import reset_for_tests

        reset_for_tests()
        mod = _reload_request_ip()

        # 公網 peer（非 10.0.0.0/8 trusted）+ 偽造 X-Real-IP
        req = _make_request(x_real_ip="8.8.8.8", client_host="203.0.113.99")
        result = mod.get_client_ip(req)
        assert result == "203.0.113.99"  # 偽造 X-Real-IP 被忽略，用真實 peer

    def test_wildcard_default_ignores_xff_returns_peer(self, monkeypatch):
        """RA-HIGH-2（2026-06-02 行為變更）：TRUSTED_PROXY_IPS='*'（未明設可信代理）
        時不再信任 X-Forwarded-For，直接回直連 peer，避免偽造 XFF 繞過 per-IP 限流。

        （原 test_wildcard_default_uses_rfc1918_as_trusted 斷言剝除後回 XFF 公網 IP；
        該行為即 RA-HIGH-2 漏洞本身，已改為安全行為。_parse_trusted_proxies 對 '*'
        仍 fallback RFC1918，見 test_wildcard_returns_rfc1918_defaults。）"""
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "*")
        from config import reset_for_tests

        reset_for_tests()
        mod = _reload_request_ip()

        req = _make_request(xff="1.2.3.4, 10.0.0.1", client_host="127.0.0.1")
        result = mod.get_client_ip(req)
        assert result == "127.0.0.1"

    def test_multiple_calls_consistent_result(self, monkeypatch):
        """多次呼叫 get_client_ip，memoize 不影響正確性（每次 request 結果一致）。"""
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.0/8")
        from config import reset_for_tests

        reset_for_tests()
        mod = _reload_request_ip()

        req = _make_request(xff="203.0.113.99, 10.0.0.1")
        results = [mod.get_client_ip(req) for _ in range(10)]
        assert all(
            r == "203.0.113.99" for r in results
        ), f"多次呼叫結果不一致：{results}"
