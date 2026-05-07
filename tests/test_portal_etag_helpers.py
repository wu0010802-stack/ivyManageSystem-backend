"""Phase 8 ETag helper unit tests。

驗證 api/portal/_shared.py 4 個 helpers 的 304 / hit / miss 行為。
endpoint integration 留 staging curl 驗證。
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock

from fastapi import Request
from fastapi.responses import Response

from api.portal._shared import (
    add_last_modified_header,
    check_etag,
    check_last_modified,
    compute_etag,
)


def _make_request(headers: dict[str, str] | None = None) -> Request:
    """建一個最小 Request mock 給 helper 測試。"""
    mock = MagicMock(spec=Request)
    mock.headers = headers or {}
    return mock


class TestComputeEtag:
    def test_returns_weak_etag_format(self):
        etag = compute_etag("hello")
        assert etag.startswith('W/"')
        assert etag.endswith('"')

    def test_same_input_same_etag(self):
        assert compute_etag("abc") == compute_etag("abc")

    def test_different_input_different_etag(self):
        assert compute_etag("abc") != compute_etag("def")

    def test_accepts_bytes(self):
        etag1 = compute_etag(b"hello")
        etag2 = compute_etag("hello")
        assert etag1 == etag2


class TestCheckEtag:
    def test_returns_304_when_if_none_match_matches(self):
        etag = compute_etag("payload")
        req = _make_request({"if-none-match": etag})
        result = check_etag(req, etag)
        assert result is not None
        assert result.status_code == 304

    def test_returns_none_when_no_if_none_match_header(self):
        etag = compute_etag("payload")
        req = _make_request({})
        result = check_etag(req, etag)
        assert result is None

    def test_returns_none_when_if_none_match_mismatches(self):
        etag = compute_etag("payload")
        req = _make_request({"if-none-match": compute_etag("other")})
        result = check_etag(req, etag)
        assert result is None


class TestCheckLastModified:
    def test_returns_304_when_if_modified_since_equal(self):
        ts = datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)
        req = _make_request({"if-modified-since": format_datetime(ts, usegmt=True)})
        result = check_last_modified(req, ts)
        assert result is not None
        assert result.status_code == 304

    def test_returns_304_when_if_modified_since_newer(self):
        server_ts = datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)
        client_ts = datetime(2026, 5, 6, 11, 0, 0, tzinfo=timezone.utc)
        req = _make_request(
            {"if-modified-since": format_datetime(client_ts, usegmt=True)}
        )
        result = check_last_modified(req, server_ts)
        assert result is not None
        assert result.status_code == 304

    def test_returns_none_when_server_newer(self):
        server_ts = datetime(2026, 5, 7, 10, 0, 0, tzinfo=timezone.utc)
        client_ts = datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)
        req = _make_request(
            {"if-modified-since": format_datetime(client_ts, usegmt=True)}
        )
        result = check_last_modified(req, server_ts)
        assert result is None

    def test_returns_none_when_no_header(self):
        ts = datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)
        req = _make_request({})
        result = check_last_modified(req, ts)
        assert result is None

    def test_returns_none_when_last_modified_is_none(self):
        req = _make_request({"if-modified-since": "Wed, 01 Jan 2020 00:00:00 GMT"})
        result = check_last_modified(req, None)
        assert result is None

    def test_handles_invalid_if_modified_since(self):
        ts = datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)
        req = _make_request({"if-modified-since": "invalid-date"})
        result = check_last_modified(req, ts)
        # 解析失敗應 fall through 回 None（不命中 304），不該 raise
        assert result is None


class TestAddLastModifiedHeader:
    def test_sets_last_modified_header_in_rfc1123_format(self):
        ts = datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)
        response = Response()
        add_last_modified_header(response, ts)
        assert "last-modified" in {k.lower(): v for k, v in response.headers.items()}
        # RFC 1123 format ends with "GMT"
        assert "GMT" in response.headers["last-modified"]
