"""utils/etag.py 單元測試。

驗證：
- compute_etag 對相同 payload 回傳相同 ETag、不同 payload 不同 ETag
- etag_response 命中 If-None-Match 回 304 + 不含 body
- etag_response 未命中時設好 ETag + Cache-Control header
- private=True 時加 private 指令；False 時不加
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import Request, Response
from fastapi.responses import Response as PlainResponse

from utils.etag import compute_etag, etag_response


def _make_request(if_none_match: str | None = None) -> Request:
    mock = MagicMock(spec=Request)
    mock.headers = {"If-None-Match": if_none_match} if if_none_match else {}
    return mock


class TestComputeEtag:
    def test_returns_quoted_hex_digest(self):
        etag = compute_etag({"a": 1})
        assert etag.startswith('"')
        assert etag.endswith('"')
        # 32-char MD5 hex 加上兩個雙引號
        assert len(etag) == 34

    def test_same_payload_same_etag(self):
        assert compute_etag({"a": 1, "b": 2}) == compute_etag({"a": 1, "b": 2})

    def test_key_order_independent(self):
        # canonical JSON (sort_keys=True) 不應因 key 順序而 ETag 不同
        assert compute_etag({"a": 1, "b": 2}) == compute_etag({"b": 2, "a": 1})

    def test_different_payload_different_etag(self):
        assert compute_etag({"a": 1}) != compute_etag({"a": 2})

    def test_accepts_bytes(self):
        # bytes 走另一條路徑（不再 json.dumps）
        result = compute_etag(b"hello")
        assert result.startswith('"') and result.endswith('"')
        assert len(result) == 34

    def test_list_payload(self):
        result = compute_etag([{"id": 1}, {"id": 2}])
        assert result.startswith('"')


class TestEtagResponse:
    def test_cache_miss_returns_payload_and_sets_headers(self):
        req = _make_request(if_none_match=None)
        resp = Response()
        payload = [{"id": 1, "name": "a"}]

        result = etag_response(req, resp, payload)

        assert result == payload
        assert "ETag" in resp.headers
        assert resp.headers["Cache-Control"] == "private, no-cache"

    def test_cache_hit_returns_304(self):
        payload = [{"id": 1}]
        etag = compute_etag(payload)
        req = _make_request(if_none_match=etag)
        resp = Response()

        result = etag_response(req, resp, payload)

        assert isinstance(result, PlainResponse)
        assert result.status_code == 304
        assert result.headers["ETag"] == etag
        # 304 不帶 body
        assert result.body == b""

    def test_stale_etag_returns_payload(self):
        payload = [{"id": 1}]
        req = _make_request(if_none_match='"stale-etag"')
        resp = Response()

        result = etag_response(req, resp, payload)

        assert result == payload
        # 新 ETag 對應目前 payload
        assert resp.headers["ETag"] == compute_etag(payload)

    def test_private_false_omits_private_directive(self):
        req = _make_request()
        resp = Response()

        etag_response(req, resp, {"x": 1}, private=False)

        assert resp.headers["Cache-Control"] == "no-cache"
        assert "private" not in resp.headers["Cache-Control"]

    def test_payload_change_invalidates_etag(self):
        # 先請求一次拿 ETag，payload 改了之後 If-None-Match 不再命中
        payload1 = [{"id": 1}]
        req1 = _make_request()
        resp1 = Response()
        etag_response(req1, resp1, payload1)
        etag1 = resp1.headers["ETag"]

        payload2 = [{"id": 1}, {"id": 2}]
        req2 = _make_request(if_none_match=etag1)
        resp2 = Response()
        result = etag_response(req2, resp2, payload2)

        # 不應 304
        assert result == payload2
        assert resp2.headers["ETag"] != etag1
