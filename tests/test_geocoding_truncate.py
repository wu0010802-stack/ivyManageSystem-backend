"""truncate_address_to_lane — 招生地址降至「巷」級。"""

import pytest

from services.geocoding_service import truncate_address_to_lane


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 樓號處理（仍可保留巷）
        ("臺北市文山區興隆路四段30巷5號3樓", "臺北市文山區興隆路四段30巷"),
        ("臺北市文山區興隆路四段30巷5號B1", "臺北市文山區興隆路四段30巷"),
        ("臺北市信義區忠孝東路五段100號", "臺北市信義區忠孝東路五段"),
        ("臺北市中正區重慶南路一段122號之2", "臺北市中正區重慶南路一段"),
        # 弄處理（保留巷）
        ("新北市板橋區文化路一段188巷5弄8號", "新北市板橋區文化路一段188巷"),
        ("新北市板橋區文化路一段188巷5弄", "新北市板橋區文化路一段188巷"),
        # 純路（無巷）— 不動
        ("臺北市大安區仁愛路四段", "臺北市大安區仁愛路四段"),
        # 樓之 + 號之
        ("高雄市三民區建工路300號3樓之2", "高雄市三民區建工路"),
        # 純巷（已 truncated）
        ("臺北市文山區興隆路四段30巷", "臺北市文山區興隆路四段30巷"),
        # 邊界：空字串
        ("", ""),
        # 邊界：未含號的地址（e.g. 地名）
        ("臺北市中正區", "臺北市中正區"),
    ],
)
def test_truncate_address_to_lane(raw: str, expected: str) -> None:
    assert truncate_address_to_lane(raw) == expected


def test_truncate_address_to_lane_preserves_lane_only() -> None:
    """巷之後若還有弄/號要剃；單純巷要保留。"""
    assert truncate_address_to_lane("臺北市X路1巷100號") == "臺北市X路1巷"
    assert truncate_address_to_lane("臺北市X路1巷") == "臺北市X路1巷"


from unittest.mock import patch, MagicMock


@patch("services.geocoding_service.requests.get")
@patch("services.geocoding_service._GOOGLE_MAPS_API_KEY", "fake")
@patch("services.geocoding_service._GEOCODING_PROVIDER", "google")
def test_geocode_google_uses_truncated_address(mock_get: MagicMock) -> None:
    """Google geocode 必須先 truncate，不送原始門牌號。"""
    from services.geocoding_service import geocode_address

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "status": "OK",
        "results": [{
            "geometry": {"location": {"lat": 25.0, "lng": 121.5}},
            "formatted_address": "臺北市文山區興隆路四段30巷"
        }]
    }
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    geocode_address("臺北市文山區興隆路四段30巷5號3樓")

    sent_address = mock_get.call_args.kwargs["params"]["address"]
    assert "5號" not in sent_address, f"門牌號未被 truncate: {sent_address}"
    assert "3樓" not in sent_address, f"樓號未被 truncate: {sent_address}"
    assert "30巷" in sent_address, f"巷層級被誤剃: {sent_address}"


@patch("services.geocoding_service.requests.get")
@patch("services.geocoding_service._GOOGLE_MAPS_API_KEY", "")
@patch("services.geocoding_service._GEOCODING_PROVIDER", "nominatim")
def test_geocode_nominatim_uses_truncated_address(mock_get: MagicMock) -> None:
    """Nominatim geocode 同樣先 truncate。"""
    from services.geocoding_service import geocode_address

    mock_resp = MagicMock()
    mock_resp.json.return_value = [{
        "lat": "25.0", "lon": "121.5",
        "display_name": "臺北市文山區興隆路四段30巷"
    }]
    mock_resp.raise_for_status = MagicMock()
    mock_get.return_value = mock_resp

    geocode_address("臺北市文山區興隆路四段30巷5號3樓")

    # nominatim 走 candidate query 第一個
    first_call_query = mock_get.call_args_list[0].kwargs["params"]["q"]
    assert "5號" not in first_call_query, f"門牌號未被 truncate: {first_call_query}"
    assert "30巷" in first_call_query
