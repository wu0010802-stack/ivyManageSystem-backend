"""招生地址 geocoding service 測試。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import geocoding_service


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class TestNominatimFallbackCandidates:
    def test_builds_reordered_and_simplified_queries_for_taiwan_address(self):
        candidates = geocoding_service._build_nominatim_query_candidates(
            "高雄市三民區大豐二路323巷8號3樓之5"
        )

        assert candidates[0] == "臺灣 高雄市三民區大豐二路323巷8號3樓之5"
        assert "大豐二路323巷8號3樓之5 三民區 高雄市" in candidates
        assert "大豐二路323巷8號 三民區 高雄市" in candidates
        assert "大豐二路 三民區 高雄市" in candidates
        assert "三民區 高雄市" in candidates

    def test_uses_fallback_query_until_nominatim_returns_a_result(self, monkeypatch):
        requested_queries = []

        def fake_get(_url, params, headers, timeout):
            requested_queries.append(params["q"])
            if params["q"] == "九如一路 819號 三民區 高雄市":
                return DummyResponse([{
                    "lat": "22.6401",
                    "lon": "120.3215",
                    "display_name": "819號, 九如一路, 安發里, 三民區, 高雄市, 807, 臺灣",
                }])
            return DummyResponse([])

        monkeypatch.setattr(geocoding_service, "_throttle_nominatim", lambda: None)
        monkeypatch.setattr(geocoding_service.requests, "get", fake_get)

        result = geocoding_service._geocode_with_nominatim("高雄市三民區九如一路819號")

        assert result == {
            "provider": "nominatim",
            "lat": 22.6401,
            "lng": 120.3215,
            "formatted_address": "819號, 九如一路, 安發里, 三民區, 高雄市, 807, 臺灣",
        }
        assert requested_queries[:2] == [
            "臺灣 高雄市三民區九如一路819號",
            "高雄市三民區九如一路819號",
        ]
        assert "九如一路 819號 三民區 高雄市" in requested_queries
