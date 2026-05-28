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
