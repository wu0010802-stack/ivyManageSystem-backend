"""P3-4 回歸（2026-06-23 全系統資安掃描）：官方行事曆同步二級 SSRF host 白名單。

services/official_calendar.py 從 data.gov.tw 回應取 resourceDownloadUrl 直接
session.get，無 scheme/host 白名單也不封鎖私有/loopback IP。上游 dataset 被
竄改 / MITM / DNS 劫持可塞 http://169.254.169.254/ 或 http://127.0.0.1:8088/，
後端排程便代為發 GET（雲端 metadata 端點 / 內網探測）。

修法：加 _is_allowed_calendar_url（https/http + 政府開放資料網域白名單 +
封鎖私有/loopback/link-local/reserved IP），_request_with_optional_ssl_fallback
在發請求前先驗。對齊 recruitment_ivykids_sync._is_allowed_sync_url 私有 IP 封鎖。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.official_calendar import (  # noqa: E402
    _is_allowed_calendar_url,
    _request_with_optional_ssl_fallback,
)

# ── 私有 / 內網 IP 封鎖 ──


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # 雲端 metadata
        "http://127.0.0.1:8088/api/internal",  # loopback
        "http://10.0.0.5/x.csv",  # 私有
        "http://192.168.1.1/x.csv",  # 私有
        "http://[::1]/x.csv",  # IPv6 loopback
    ],
)
def test_rejects_private_loopback_ip(url):
    assert _is_allowed_calendar_url(url) is False


# ── 非政府網域 / 非 http(s) ──


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com/x.csv",
        "https://data.gov.tw.attacker.com/x.csv",  # 後綴混淆
        "file:///etc/passwd",
        "ftp://data.gov.tw/x.csv",
        "",
    ],
)
def test_rejects_non_gov_or_non_http(url):
    assert _is_allowed_calendar_url(url) is False


# ── 政府開放資料網域放行 ──


@pytest.mark.parametrize(
    "url",
    [
        "https://www.dgpa.gov.tw/cp.aspx?n=x",
        "https://quality.data.gov.tw/dq_download_csv.php?nid=14718",
        "https://data.gov.tw/api/v2/rest/dataset/14718",
    ],
)
def test_allows_gov_open_data(url):
    assert _is_allowed_calendar_url(url) is True


# ── 請求前守衛：不允許的 URL 不應發出請求 ──


def test_request_rejects_disallowed_url_before_fetch():
    with pytest.raises(ValueError):
        _request_with_optional_ssl_fallback("http://169.254.169.254/latest/")
