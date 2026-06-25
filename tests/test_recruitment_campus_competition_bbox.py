"""campus-competition haversine bounding-box 預篩（P2 效能修補）。

get_campus_competition 對每個常春藤校區跑 raw SQL haversine 掃整張 competitor_school
（trig 運算式，btree 用不上 → 每圈全表 seq scan + 全表 trig）。加 lat/lng
bounding-box 預篩：box ⊇ 6km 圓，先以 cheap BETWEEN 過濾、AND 短路使 trig 只算
box 內列；原 raw haversine <= 6 不動 → 結果與全表 haversine 完全等價。

端點 SQL 為 PostgreSQL 專屬（acos/cos/radians/::numeric），SQLite 測試環境無法執行；
故對正確性關鍵的純函式 _bounding_box 做單元測試（box 必為圓的超集），SQL 的精確
過濾仍由 raw haversine <= 6 保證。
"""

import math

from api.recruitment.competitors import _bounding_box

# 高雄座標（系統僅抓高雄市；cos(lat)≈0.92）
_LAT, _LNG = 22.6, 120.3
_RADIUS = 6.0


def _haversine_km(lat1, lng1, lat2, lng2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlng = math.radians(lng2 - lng1)
    return r * math.acos(
        min(
            1.0,
            math.cos(p1) * math.cos(p2) * math.cos(dlng) + math.sin(p1) * math.sin(p2),
        )
    )


def test_box_contains_center():
    lat_min, lat_max, lng_min, lng_max = _bounding_box(_LAT, _LNG, _RADIUS)
    assert lat_min < _LAT < lat_max
    assert lng_min < _LNG < lng_max


def test_box_contains_all_points_within_radius():
    """半徑內任一點（含四方位極值）都必須落在 box（box ⊇ 圓，預篩不漏）。"""
    lat_min, lat_max, lng_min, lng_max = _bounding_box(_LAT, _LNG, _RADIUS)
    # 四方位 + 對角，距圓心 = 半徑的點
    cos_lat = math.cos(math.radians(_LAT))
    pts = [
        (_LAT + _RADIUS / 111.0, _LNG),  # 北
        (_LAT - _RADIUS / 111.0, _LNG),  # 南
        (_LAT, _LNG + _RADIUS / (111.0 * cos_lat)),  # 東
        (_LAT, _LNG - _RADIUS / (111.0 * cos_lat)),  # 西
    ]
    for plat, plng in pts:
        # 確認這些點確實在 ~半徑內（sanity）
        assert _haversine_km(_LAT, _LNG, plat, plng) <= _RADIUS + 0.2
        assert lat_min <= plat <= lat_max, f"({plat},{plng}) lat 落在 box 外"
        assert lng_min <= plng <= lng_max, f"({plat},{plng}) lng 落在 box 外"


def test_box_excludes_far_points():
    """box 不應無界放大：距圓心 2×半徑的點應落在 box 外（證明預篩有篩選力）。"""
    lat_min, lat_max, lng_min, lng_max = _bounding_box(_LAT, _LNG, _RADIUS)
    far_north = _LAT + 2 * _RADIUS / 111.0
    assert not (lat_min <= far_north <= lat_max)
