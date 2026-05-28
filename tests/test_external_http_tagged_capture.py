"""Phase 1 P1 resilience：3 個外部 HTTP service 例外須呼叫 tagged_capture."""

from unittest.mock import MagicMock, patch

import pytest


class TestRecruitmentMarketIntelligence:
    """3 helper functions: _request_json / _request_text / _post_json."""

    def test_request_json_exception_tagged_capture(self, monkeypatch):
        """_request_json — ConnectionError → tagged_capture(tag='external_http')."""
        import services.recruitment_market_intelligence as rmi

        monkeypatch.setattr(
            "services.recruitment_market_intelligence.requests.get",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("net")),
        )
        with patch(
            "services.recruitment_market_intelligence.tagged_capture"
        ) as mock_capture:
            with pytest.raises(ConnectionError):
                rmi._request_json("https://example.com/api")
            mock_capture.assert_called_once()
            call_args = mock_capture.call_args
            assert (
                call_args.kwargs.get("tag") == "external_http"
                or call_args.args[1] == "external_http"
            )
            assert call_args.kwargs.get("level") == "error" or "error" in call_args.args

    def test_request_text_exception_tagged_capture(self, monkeypatch):
        """_request_text — ConnectionError → tagged_capture(tag='external_http')."""
        import services.recruitment_market_intelligence as rmi

        monkeypatch.setattr(
            "services.recruitment_market_intelligence.requests.get",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("net")),
        )
        with patch(
            "services.recruitment_market_intelligence.tagged_capture"
        ) as mock_capture:
            with pytest.raises(ConnectionError):
                rmi._request_text("https://example.com/api")
            mock_capture.assert_called_once()

    def test_post_json_exception_tagged_capture(self, monkeypatch):
        """_post_json — ConnectionError → tagged_capture(tag='external_http')."""
        import services.recruitment_market_intelligence as rmi

        monkeypatch.setattr(
            "services.recruitment_market_intelligence.requests.post",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("net")),
        )
        with patch(
            "services.recruitment_market_intelligence.tagged_capture"
        ) as mock_capture:
            with pytest.raises(ConnectionError):
                rmi._post_json("https://example.com/api", payload={"x": 1})
            mock_capture.assert_called_once()

    def test_success_no_capture(self, monkeypatch):
        """正常回應不呼叫 tagged_capture."""
        import services.recruitment_market_intelligence as rmi

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"ok": True}
        monkeypatch.setattr(
            "services.recruitment_market_intelligence.requests.get",
            lambda *a, **k: mock_resp,
        )
        with patch(
            "services.recruitment_market_intelligence.tagged_capture"
        ) as mock_capture:
            result = rmi._request_json("https://example.com/api")
            assert result == {"ok": True}
            mock_capture.assert_not_called()


class TestGeocodingService:
    """2 functions: _geocode_with_google / _geocode_with_nominatim."""

    def test_geocode_with_google_exception_tagged_capture(self, monkeypatch):
        """_geocode_with_google — ConnectionError → tagged_capture(tag='external_http')."""
        import services.geocoding_service as geo

        # Ensure API key is set so the early guard doesn't skip
        monkeypatch.setattr(geo, "_GOOGLE_MAPS_API_KEY", "fake-key")
        monkeypatch.setattr(
            "services.geocoding_service.requests.get",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("net")),
        )
        with patch("services.geocoding_service.tagged_capture") as mock_capture:
            with pytest.raises(ConnectionError):
                geo._geocode_with_google("台北市信義區")
            mock_capture.assert_called_once()
            call_args = mock_capture.call_args
            assert (
                call_args.kwargs.get("tag") == "external_http"
                or call_args.args[1] == "external_http"
            )

    def test_geocode_with_nominatim_exception_tagged_capture(self, monkeypatch):
        """_geocode_with_nominatim — ConnectionError → tagged_capture(tag='external_http')."""
        import services.geocoding_service as geo

        monkeypatch.setattr(
            "services.geocoding_service.requests.get",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("net")),
        )
        monkeypatch.setattr(
            "services.geocoding_service._throttle_nominatim",
            lambda: None,
        )
        with patch("services.geocoding_service.tagged_capture") as mock_capture:
            with pytest.raises(ConnectionError):
                geo._geocode_with_nominatim("台北市信義區")
            mock_capture.assert_called_once()

    def test_success_no_capture_google(self, monkeypatch):
        """_geocode_with_google 正常回應不呼叫 tagged_capture."""
        import services.geocoding_service as geo

        monkeypatch.setattr(geo, "_GOOGLE_MAPS_API_KEY", "fake-key")
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "status": "OK",
            "results": [
                {
                    "geometry": {"location": {"lat": 25.0, "lng": 121.0}},
                    "formatted_address": "台北市信義區",
                }
            ],
        }
        monkeypatch.setattr(
            "services.geocoding_service.requests.get",
            lambda *a, **k: mock_resp,
        )
        with patch("services.geocoding_service.tagged_capture") as mock_capture:
            result = geo._geocode_with_google("台北市信義區")
            assert result is not None
            assert result["provider"] == "google"
            mock_capture.assert_not_called()


class TestOfficialCalendar:
    """1 function: _get_resource_metadata."""

    def test_get_resource_metadata_exception_tagged_capture(self, monkeypatch):
        """_get_resource_metadata — ConnectionError → tagged_capture(tag='external_http')."""
        import services.official_calendar as cal

        monkeypatch.setattr(
            "services.official_calendar.requests.get",
            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("net")),
        )
        with patch("services.official_calendar.tagged_capture") as mock_capture:
            with pytest.raises(ConnectionError):
                cal._get_resource_metadata(2024)
            mock_capture.assert_called_once()
            call_args = mock_capture.call_args
            assert (
                call_args.kwargs.get("tag") == "external_http"
                or call_args.args[1] == "external_http"
            )

    def test_success_no_capture(self, monkeypatch):
        """_get_resource_metadata 正常回應不呼叫 tagged_capture."""
        import services.official_calendar as cal

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "result": {
                "distribution": [
                    {
                        "resourceDownloadUrl": "https://example.com/data.csv",
                        "resourceQualityCheckTime": "2024-01-01",
                        "resourceDescription": "113年",
                    }
                ]
            }
        }
        monkeypatch.setattr(
            "services.official_calendar.requests.get",
            lambda *a, **k: mock_resp,
        )
        # _select_official_distribution picks from distribution list
        monkeypatch.setattr(
            "services.official_calendar._select_official_distribution",
            lambda distribution, minguo_year: {
                "resourceDownloadUrl": "https://example.com/data.csv",
                "resourceQualityCheckTime": "2024-01-01",
                "resourceDescription": "113年",
            },
        )
        with patch("services.official_calendar.tagged_capture") as mock_capture:
            result = cal._get_resource_metadata(2024)
            assert result["download_url"] == "https://example.com/data.csv"
            mock_capture.assert_not_called()
