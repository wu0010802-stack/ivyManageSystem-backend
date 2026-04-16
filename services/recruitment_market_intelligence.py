"""招生生活圈 / 市場情報服務。"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

import requests
from sqlalchemy import func

from models.recruitment import (
    CompetitorSchool,
    RecruitmentAreaInsightCache,
    RecruitmentCampusSetting,
    RecruitmentGeocodeCache,
    RecruitmentVisit,
)
from services.geocoding_service import current_geocoding_provider, geocode_address

logger = logging.getLogger(__name__)

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
GOOGLE_GEOCODING_URL = os.environ.get(
    "GOOGLE_GEOCODING_URL",
    "https://maps.googleapis.com/maps/api/geocode/json",
).strip()
GOOGLE_ROUTES_API_URL = os.environ.get(
    "GOOGLE_ROUTES_API_URL",
    "https://routes.googleapis.com/directions/v2:computeRoutes",
).strip()
GOOGLE_PLACES_TEXT_API_URL = os.environ.get(
    "GOOGLE_PLACES_TEXT_API_URL",
    "https://places.googleapis.com/v1/places:searchText",
).strip()
TGOS_QUERY_ADDR_URL = os.environ.get(
    "TGOS_QUERY_ADDR_URL",
    "http://gis.tgos.tw/addrws/v30/QueryAddr.asmx/QueryAddr",
).strip()
TGOS_APP_ID = os.environ.get("TGOS_APP_ID", "").strip()
TGOS_API_KEY = os.environ.get("TGOS_API_KEY", "").strip()
TGOS_ROUTE_URL = os.environ.get(
    "TGOS_ROUTE_URL",
    "http://gis.tgos.tw/TGRoute/TGRoute.aspx",
).strip()
NLSC_TOWN_QUERY_URL = os.environ.get(
    "NLSC_TOWN_QUERY_URL",
    "https://api.nlsc.gov.tw/other/TownVillagePointQuery",
).strip()
NLSC_LAND_USE_URL = os.environ.get(
    "NLSC_LAND_USE_URL",
    "https://api.nlsc.gov.tw/other/LandUsePointQuery",
).strip()
RECRUITMENT_POPULATION_DENSITY_URL = os.environ.get(
    "RECRUITMENT_POPULATION_DENSITY_URL", ""
).strip()
RECRUITMENT_POPULATION_AGE_URL = os.environ.get(
    "RECRUITMENT_POPULATION_AGE_URL", ""
).strip()
RECRUITMENT_CAMPUS_NAME = (
    os.environ.get("RECRUITMENT_CAMPUS_NAME", "本園").strip() or "本園"
)
RECRUITMENT_CAMPUS_ADDRESS = os.environ.get("RECRUITMENT_CAMPUS_ADDRESS", "").strip()
RECRUITMENT_CAMPUS_LAT = os.environ.get("RECRUITMENT_CAMPUS_LAT", "").strip()
RECRUITMENT_CAMPUS_LNG = os.environ.get("RECRUITMENT_CAMPUS_LNG", "").strip()
RECRUITMENT_CAMPUS_TRAVEL_MODE = (
    os.environ.get("RECRUITMENT_CAMPUS_TRAVEL_MODE", "driving").strip() or "driving"
)
REQUEST_TIMEOUT = float(os.environ.get("RECRUITMENT_MARKET_TIMEOUT_SECONDS", "12"))
GOOGLE_PLACES_PAGE_SIZE = 20
GOOGLE_PLACES_MAX_RESULTS = 60  # Google Places New Text Search 每次上限
# MOE gap-fill：每次 request 最多對新學校呼叫 geocoding API 的次數上限
MOE_GEOCODE_PER_REQUEST_LIMIT = 5
DATASET_SCOPE_ALL = "all"

# ── MOE 學校名稱比對 helpers ────────────────────────────────────────────────
_SCHOOL_SUFFIX_RE = re.compile(r"幼[兒儿]園|幼稚園")
_SCHOOL_GEO_PREFIX_RE = re.compile(
    r"^[\u4e00-\u9fa5]{2,5}[市縣]([\u4e00-\u9fa5]{1,4}[區鄉鎮])?"
)
_SCHOOL_TYPE_PREFIX_RE = re.compile(r"^(公立|私立|非營利|準公共)+")
MATCH_THRESHOLD = 80
HIGH_CONFIDENCE = 120


def _norm_school_name(name: str) -> str:
    """正規化學校名稱：去空白/全形、統一臺→台、統一幼稚園→幼兒園、去括號附註。"""
    s = str(name or "").strip()
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = re.sub(r"[\s\u3000]+", "", s)
    return s.replace("臺", "台").replace("幼稚園", "幼兒園").lower()


def _strip_school_prefix(name: str) -> str:
    """去除縣市/行政區/設立別前綴，取得純校名。"""
    s = _SCHOOL_GEO_PREFIX_RE.sub("", name)
    return _SCHOOL_TYPE_PREFIX_RE.sub("", s).strip()


def _extract_candidates(google_name: str) -> list[str]:
    """從 Google Places 顯示名稱提取多個候選校名。

    例如 "吉的堡資優幼兒園高雄光武分園-三民美語幼兒園｜雙語教學｜幼兒美語"
    → ["吉的堡資優幼兒園高雄光武分園-三民美語幼兒園",
       "吉的堡資優幼兒園高雄光武分園", "三民美語幼兒園"]
    """
    raw = str(google_name or "").strip()
    if not raw:
        return []

    # 1. 以 ｜| 切割，只保留含幼兒園/幼稚園的片段
    segments = re.split(r"[｜|]", raw)
    base = segments[0].strip()

    candidates: list[str] = []
    # 2. 完整名稱（去括號）
    cleaned = re.sub(r"[（(][^）)]*[）)]", "", base).strip()
    if cleaned:
        candidates.append(cleaned)

    # 3. 以 -－–— 切割，每段都是候選
    parts = re.split(r"[-－–—]", cleaned)
    for part in parts:
        part = part.strip()
        if len(part) >= 3 and part not in candidates:
            candidates.append(part)

    # 4. 其他 ｜ 片段中含幼兒園的也加入
    for seg in segments[1:]:
        seg = seg.strip()
        if (
            ("幼兒園" in seg or "幼稚園" in seg)
            and len(seg) >= 3
            and seg not in candidates
        ):
            candidates.append(seg)

    return candidates


def _score_name(candidate: str, moe_name: str) -> int:
    """名稱相似度評分（0-100）。"""
    nc = _norm_school_name(candidate)
    nm = _norm_school_name(moe_name)
    if not nc or not nm:
        return 0
    # 1. 正規化精確
    if nc == nm:
        return 100
    sc = _strip_school_prefix(nc)
    sm = _strip_school_prefix(nm)
    # 2. 去前綴精確
    if sc and sm and sc == sm:
        return 90
    # 3. 包含（≥4 字元，比舊版的 3 嚴格）
    shorter, longer = (nc, nm) if len(nc) <= len(nm) else (nm, nc)
    if len(shorter) >= 4 and shorter in longer:
        return 75
    shorter_s, longer_s = (sc, sm) if len(sc) <= len(sm) else (sm, sc)
    if len(shorter_s) >= 4 and shorter_s in longer_s:
        return 65
    # 4. 3 字元滑動視窗（降級為輔助信號）
    cc = _SCHOOL_SUFFIX_RE.sub("", sc)
    cm = _SCHOOL_SUFFIX_RE.sub("", sm)
    if len(cc) >= 3 and len(cm) >= 3:
        s_short, s_long = (cc, cm) if len(cc) <= len(cm) else (cm, cc)
        for i in range(len(s_short) - 2):
            if s_short[i : i + 3] in s_long:
                return 40
    return 0


def _score_match(
    candidate: str,
    moe,
    google_address: str,
    google_lat: float | None,
    google_lng: float | None,
) -> int:
    """多信號評分：名稱 + 地址 + 距離。"""
    name_score = _score_name(candidate, moe.school_name)
    if name_score == 0:
        return 0

    # 地址加分
    address_score = 0
    g_district = _extract_district_hint(google_address)
    if g_district:
        if g_district in (moe.district or ""):
            address_score = 25
        else:
            address_score = -30

    # 距離加分（需 MOE 有座標）
    distance_score = 0
    if google_lat and google_lng and moe.latitude and moe.longitude:
        dist = _haversine_km(google_lat, google_lng, moe.latitude, moe.longitude)
        if dist < 0.2:
            distance_score = 50
        elif dist < 0.5:
            distance_score = 30
        elif dist < 1.0:
            distance_score = 15
        elif dist > 3.0:
            distance_score = -20

    return name_score + address_score + distance_score


def _extract_district_hint(address: str) -> str:
    m = re.search(r"([\u4e00-\u9fa5]{1,4}[區鄉鎮])", str(address or ""))
    return m.group(1) if m else ""


def _find_moe_match(
    google_name: str,
    google_address: str,
    moe_schools: list,
    google_place_id: str | None = None,
    google_lat: float | None = None,
    google_lng: float | None = None,
) -> tuple[Any, int]:
    """從 moe_schools 找出與 Google 學校最佳比對的 CompetitorSchool 記錄。

    回傳 (matched_school, confidence_score)。
    """
    if not moe_schools or not google_name:
        return None, 0

    # 1. place_id 快取命中 → O(1)
    if google_place_id:
        cached = next(
            (s for s in moe_schools if s.google_place_id == google_place_id), None
        )
        if cached:
            return cached, 100

    # 2. 提取候選名，逐一評分
    candidates = _extract_candidates(google_name)
    if not candidates:
        candidates = [google_name]

    best_school, best_score = None, 0
    for candidate in candidates:
        for moe in moe_schools:
            score = _score_match(candidate, moe, google_address, google_lat, google_lng)
            if score > best_score:
                best_score = score
                best_school = moe

    if best_score >= MATCH_THRESHOLD:
        return best_school, best_score
    return None, 0


def _enrich_from_moe(moe: Any) -> dict[str, Any]:
    """將 CompetitorSchool 記錄轉為 enrichment 欄位 dict。"""
    return {
        "db_id": moe.id,
        "school_type": moe.school_type,
        "pre_public_type": moe.pre_public_type,
        "phone": moe.phone,
        "approved_capacity": moe.approved_capacity,
        "monthly_fee": moe.monthly_fee,
        "has_penalty": bool(moe.has_penalty),
        "is_active": bool(moe.is_active),
        "owner_name": moe.owner_name,
        "approved_date": moe.approved_date,
        "total_area_sqm": moe.total_area_sqm,
        "website": moe.website,
        "indoor_area_sqm": getattr(moe, "indoor_area_sqm", None),
        "outdoor_area_sqm": getattr(moe, "outdoor_area_sqm", None),
        "floor": getattr(moe, "floor_info", None),
        "shuttle": getattr(moe, "shuttle_info", None),
        "has_after_school": bool(getattr(moe, "has_after_school", False)),
        "source": "google",
    }


def _infer_school_type_from_name(name: str) -> str | None:
    """從學校名稱推斷類型（公立/私立/非營利），作為 DB 比對失敗時的 fallback。"""
    n = str(name or "")
    if "非營利" in n:
        return "非營利"
    if re.search(
        r"^(高雄市|台南市|台北市|新北市|台中市|桃園市|[\u4e00-\u9fa5]{2,4}[市縣])\s*[^\s]*[區鄉鎮]?\s*(立)?",
        n,
    ):
        if "私立" not in n:
            if re.match(r"^[\u4e00-\u9fa5]{2,5}[市縣]", n) and "私" not in n:
                return "公立"
    if "私立" in n:
        return "私立"
    if "公立" in n or "市立" in n or "國立" in n:
        return "公立"
    if "準公共" in n:
        return "私立"
    return None


def _empty_enrichment(google_name: str = "") -> dict[str, Any]:
    return {
        "db_id": None,
        "school_type": _infer_school_type_from_name(google_name),
        "pre_public_type": None,
        "phone": None,
        "approved_capacity": None,
        "monthly_fee": None,
        "has_penalty": False,
        "is_active": True,
        "owner_name": None,
        "approved_date": None,
        "total_area_sqm": None,
        "website": None,
        "indoor_area_sqm": None,
        "outdoor_area_sqm": None,
        "floor": None,
        "shuttle": None,
        "has_after_school": False,
        "source": "google",
    }


def _moe_full_address(moe: Any) -> str:
    """組合 MOE 學校完整地址（縣市＋行政區＋地址）。"""
    city = (moe.city or "").strip()
    district = (moe.district or "").strip()
    addr = (moe.address or "").strip()
    # 若 addr 已包含縣市/區，直接回傳；否則補齊前綴
    if addr.startswith(city) or addr.startswith(district):
        return addr
    return f"{city}{district}{addr}"


def _resolve_moe_coords(session, moe: Any) -> tuple[Optional[float], Optional[float]]:
    """取得 MOE 學校座標（三段式查找）：
    1. competitor_school.latitude/longitude（已存 DB）
    2. RecruitmentGeocodeCache（短地址 or 完整地址）
    """
    if moe.latitude is not None and moe.longitude is not None:
        return float(moe.latitude), float(moe.longitude)
    # 嘗試短地址與完整地址都查快取
    full_addr = _moe_full_address(moe)
    for addr_key in {moe.address, full_addr} - {None, ""}:
        cached = (
            session.query(RecruitmentGeocodeCache)
            .filter(
                RecruitmentGeocodeCache.address == addr_key,
                RecruitmentGeocodeCache.status == "resolved",
            )
            .first()
        )
        if cached and cached.lat is not None and cached.lng is not None:
            return float(cached.lat), float(cached.lng)
    return None, None


def _geocode_and_save_moe(session, moe: Any) -> tuple[Optional[float], Optional[float]]:
    """對 MOE 學校地址呼叫 geocoding API，將座標存回 competitor_school 表。
    使用完整地址（縣市＋行政區＋地址）以提高命中率。
    """
    full_addr = _moe_full_address(moe)
    if not full_addr:
        return None, None
    result = geocode_address(full_addr)
    if result and result.get("lat") and result.get("lng"):
        lat, lng = float(result["lat"]), float(result["lng"])
        try:
            moe.latitude = lat
            moe.longitude = lng
            session.flush()
            logger.info(
                "MOE 學校座標 geocode 完成：%s addr=%s lat=%s lng=%s",
                moe.school_name,
                full_addr,
                lat,
                lng,
            )
        except Exception as exc:
            logger.warning(
                "儲存 MOE 學校座標失敗 school=%s err=%s", moe.school_name, exc
            )
            session.rollback()
        return lat, lng
    return None, None


def geocode_all_competitor_schools(session, *, limit: int = 100) -> dict[str, Any]:
    """批量 geocode 所有尚無座標的 competitor_school 記錄（供管理員手動觸發）。
    回傳 {total, geocoded, failed, skipped}。
    """
    schools = (
        session.query(CompetitorSchool)
        .filter(
            CompetitorSchool.latitude.is_(None),
            CompetitorSchool.city.like("%高雄%"),
            CompetitorSchool.is_active == True,  # noqa: E712
        )
        .limit(limit)
        .all()
    )
    geocoded = failed = 0
    for moe in schools:
        lat, lng = _geocode_and_save_moe(session, moe)
        if lat is not None:
            geocoded += 1
        else:
            failed += 1
    return {
        "total": len(schools),
        "geocoded": geocoded,
        "failed": failed,
        "skipped": 0,
    }


TRAVEL_SPEEDS_KMH = {
    "driving": 30.0,
    "walking": 4.5,
    "cycling": 15.0,
}

SUPPORTED_TRAVEL_MODES = set(TRAVEL_SPEEDS_KMH)
GOOGLE_TRAVEL_MODES = {
    "driving": "DRIVE",
    "walking": "WALK",
    "cycling": "BICYCLE",
}

_DENSITY_KEYS = ("population_density", "人口密度")
_DISTRICT_KEYS = ("site_id", "區域別", "townName", "TOWNNAME", "town_name")


def _normalize_text(value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    return (
        text.replace("台灣", "臺灣")
        .replace("台北", "臺北")
        .replace("台中", "臺中")
        .replace("台南", "臺南")
        .replace("台東", "臺東")
    )


def _normalize_area_key(value: Any) -> str:
    return _normalize_text(value).replace(" ", "")


def _normalize_dataset_scope(dataset_scope: Optional[str]) -> str:
    return DATASET_SCOPE_ALL


def _apply_dataset_scope(query, dataset_scope: Optional[str]):
    return query


def _scoped_visit_query(session, dataset_scope: Optional[str] = None):
    return _apply_dataset_scope(session.query(RecruitmentVisit), dataset_scope)


def _scoped_hotspot_addresses(
    session, dataset_scope: Optional[str] = None
) -> Optional[list[str]]:
    return None


def _extract_county_district(
    address: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    text = _normalize_text(address)
    if not text:
        return None, None
    matched = re.match(r"(?P<county>.+?[縣市])(?P<district>.+?(?:區|鄉|鎮|市))", text)
    if not matched:
        return None, None
    return matched.group("county"), matched.group("district")


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _extract_first_value(
    row: dict[str, Any], candidate_keys: Iterable[str]
) -> Optional[str]:
    for key in candidate_keys:
        if key in row and row[key] not in (None, ""):
            return str(row[key]).strip()
    for existing_key, value in row.items():
        normalized_key = _normalize_area_key(existing_key)
        for candidate in candidate_keys:
            if normalized_key == _normalize_area_key(candidate) and value not in (
                None,
                "",
            ):
                return str(value).strip()
    return None


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("responseData", "records", "data", "result", "Data", "Rows", "row"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                extracted = _extract_records(value)
                if extracted:
                    return extracted
        for value in payload.values():
            extracted = _extract_records(value)
            if extracted:
                return extracted
    return []


def _extract_xml_text(root: ET.Element, candidate_keys: Iterable[str]) -> Optional[str]:
    normalized = {_normalize_area_key(key) for key in candidate_keys}
    for element in root.iter():
        tag = element.tag.split("}")[-1]
        if _normalize_area_key(tag) in normalized:
            text = _normalize_text(element.text)
            if text:
                return text
    return None


def _request_json(url: str, *, params: Optional[dict[str, Any]] = None) -> Any:
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _request_text(url: str, *, params: Optional[dict[str, Any]] = None) -> str:
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def _post_json(
    url: str, *, payload: dict[str, Any], headers: Optional[dict[str, str]] = None
) -> Any:
    response = requests.post(
        url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def _radius_to_bounds(lat: float, lng: float, radius_km: float) -> dict:
    """將圓心 + 半徑轉換為 Google Places API 接受的 rectangle bounds。
    Google locationRestriction 只支援 rectangle，不支援 circle。
    """
    from math import cos, radians

    lat_delta = radius_km / 111.32
    lng_delta = radius_km / (111.32 * cos(radians(lat)))
    return {
        "low": {"latitude": lat - lat_delta, "longitude": lng - lng_delta},
        "high": {"latitude": lat + lat_delta, "longitude": lng + lng_delta},
    }


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    from math import atan2, cos, radians, sin, sqrt

    radius = 6371.0
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    )
    return radius * 2 * atan2(sqrt(a), sqrt(1 - a))


def _estimate_travel(
    distance_km: Optional[float], mode: str
) -> tuple[Optional[float], Optional[float], str]:
    if distance_km is None:
        return None, None, "partial"
    speed = TRAVEL_SPEEDS_KMH.get(mode, TRAVEL_SPEEDS_KMH["driving"])
    minutes = round((distance_km / speed) * 60, 1) if speed else None
    return round(distance_km, 2), minutes, "estimated"


def _extract_numeric_from_payload(payload: Any, *patterns: str) -> Optional[float]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(pattern in lowered for pattern in patterns):
                num = _safe_float(value)
                if num is not None:
                    return num
            nested = _extract_numeric_from_payload(value, *patterns)
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _extract_numeric_from_payload(item, *patterns)
            if nested is not None:
                return nested
    return None


def _normalize_google_query_address(address: str) -> str:
    normalized = _normalize_text(address)
    if normalized and "臺灣" not in normalized and "台灣" not in normalized:
        normalized = f"臺灣 {normalized}"
    return normalized


def _google_available() -> bool:
    return bool(GOOGLE_MAPS_API_KEY)


def _google_places_api_available() -> bool:
    return bool(GOOGLE_MAPS_API_KEY)


def _tgos_available() -> bool:
    return bool(TGOS_APP_ID and TGOS_API_KEY)


def current_market_provider() -> Optional[str]:
    if _google_available():
        return "google"
    if _tgos_available():
        return "tgos"
    return current_geocoding_provider()


def market_provider_available() -> bool:
    return current_market_provider() is not None


def _query_google_address(address: str) -> Optional[dict[str, Any]]:
    if not _google_available():
        return None

    payload = _request_json(
        GOOGLE_GEOCODING_URL,
        params={
            "address": _normalize_google_query_address(address),
            "key": GOOGLE_MAPS_API_KEY,
            "language": "zh-TW",
            "region": "tw",
        },
    )
    results = payload.get("results") or []
    if payload.get("status") != "OK" or not results:
        logger.warning(
            "Google geocoding 無結果: status=%s address=%s",
            payload.get("status"),
            address,
        )
        return None

    top = results[0]
    location = top.get("geometry", {}).get("location", {})
    lat = _safe_float(location.get("lat"))
    lng = _safe_float(location.get("lng"))
    if lat is None or lng is None:
        return None

    return {
        "lat": lat,
        "lng": lng,
        "formatted_address": top.get("formatted_address") or _normalize_text(address),
        "matched_address": top.get("formatted_address") or _normalize_text(address),
        "google_place_id": top.get("place_id"),
        "provider": "google",
        "raw": top,
    }


def _parse_google_duration_seconds(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    matched = re.fullmatch(r"(?P<seconds>\d+(?:\.\d+)?)s", str(value).strip())
    if not matched:
        return None
    return float(matched.group("seconds"))


def _query_tgos_address(
    address: str, srs: str = "EPSG:4326"
) -> Optional[dict[str, Any]]:
    if not _tgos_available():
        return None

    payload = _request_json(
        TGOS_QUERY_ADDR_URL,
        params={
            "oAPPId": TGOS_APP_ID,
            "oAPIKey": TGOS_API_KEY,
            "oSRS": srs,
            "oResultDataType": "json",
            "oAddress": _normalize_text(address),
            "oFuzzyType": 0,
            "oFuzzyBuffer": 0,
            "oIsOnlyFullMatch": "false",
            "oIsSameNumber_SubNumber": "true",
            "oCanIgnoreVillage": "true",
            "oCanIgnoreNeighborhood": "true",
            "oReturnMaxCount": 1,
        },
    )
    records = _extract_records(payload)
    if not records:
        return None

    top = records[0]
    lng = _safe_float(top.get("X") or top.get("x"))
    lat = _safe_float(top.get("Y") or top.get("y"))
    matched_address = _extract_first_value(top, ("FULL_ADDR", "FULLADDRESS", "address"))
    return {
        "lat": lat,
        "lng": lng,
        "formatted_address": matched_address or _normalize_text(address),
        "matched_address": matched_address or _normalize_text(address),
        "google_place_id": None,
        "provider": "tgos",
        "raw": top,
    }


def _query_tgos_route(
    campus_point: dict[str, Any], target_point: dict[str, Any]
) -> tuple[Optional[float], Optional[float], str]:
    if not _tgos_available():
        return None, None, "partial"

    campus_xy = (
        _query_tgos_address(campus_point["campus_address"], srs="EPSG:3826")
        if campus_point.get("campus_address")
        else None
    )
    target_xy = _query_tgos_address(target_point["matched_address"], srs="EPSG:3826")
    if not campus_xy or not target_xy:
        return None, None, "partial"

    x1 = _safe_float(campus_xy["raw"].get("X"))
    y1 = _safe_float(campus_xy["raw"].get("Y"))
    x2 = _safe_float(target_xy["raw"].get("X"))
    y2 = _safe_float(target_xy["raw"].get("Y"))
    if None in {x1, y1, x2, y2}:
        return None, None, "partial"

    try:
        payload = _request_json(
            TGOS_ROUTE_URL,
            params={
                "oAPPId": TGOS_APP_ID,
                "oAPIKey": TGOS_API_KEY,
                "waypoints": f"MULTIPOINT({x1} {y1},{x2} {y2})",
                "format": "json",
                "avoidhighway": "false",
            },
        )
    except Exception as exc:
        logger.warning("TGOS route 查詢失敗：%s", exc)
        return None, None, "partial"

    distance_m = _extract_numeric_from_payload(payload, "distance", "length")
    duration_sec = _extract_numeric_from_payload(
        payload, "time", "duration", "traveltime"
    )
    if distance_m is None and duration_sec is None:
        return None, None, "partial"

    distance_km = round((distance_m or 0) / 1000, 2) if distance_m is not None else None
    duration_min = (
        round((duration_sec or 0) / 60, 1) if duration_sec is not None else None
    )
    return distance_km, duration_min, "complete"


def _query_google_route(
    campus_point: dict[str, Any],
    target_point: dict[str, Any],
) -> tuple[Optional[float], Optional[float], str]:
    if not _google_available():
        return None, None, "partial"

    travel_mode = campus_point.get("travel_mode") or "driving"
    google_travel_mode = GOOGLE_TRAVEL_MODES.get(
        travel_mode, GOOGLE_TRAVEL_MODES["driving"]
    )
    payload: dict[str, Any] = {
        "origin": {
            "location": {
                "latLng": {
                    "latitude": campus_point["campus_lat"],
                    "longitude": campus_point["campus_lng"],
                }
            }
        },
        "destination": {
            "location": {
                "latLng": {
                    "latitude": target_point["lat"],
                    "longitude": target_point["lng"],
                }
            }
        },
        "travelMode": google_travel_mode,
        "languageCode": "zh-TW",
        "units": "METRIC",
    }
    if google_travel_mode == "DRIVE":
        payload["routingPreference"] = "TRAFFIC_UNAWARE"

    try:
        response = _post_json(
            GOOGLE_ROUTES_API_URL,
            payload=payload,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
            },
        )
    except Exception as exc:
        logger.warning("Google Routes 查詢失敗：%s", exc)
        return None, None, "partial"

    routes = response.get("routes") or []
    if not routes:
        return None, None, "partial"

    route = routes[0]
    distance_m = _safe_float(route.get("distanceMeters"))
    duration_sec = _parse_google_duration_seconds(route.get("duration"))
    if distance_m is None and duration_sec is None:
        return None, None, "partial"

    distance_km = round((distance_m or 0) / 1000, 2) if distance_m is not None else None
    duration_min = (
        round((duration_sec or 0) / 60, 1) if duration_sec is not None else None
    )
    return distance_km, duration_min, "complete"


def _query_google_places_text(
    payload: dict[str, Any], *, field_mask: str
) -> dict[str, Any]:
    return _post_json(
        GOOGLE_PLACES_TEXT_API_URL,
        payload=payload,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
            "X-Goog-FieldMask": field_mask,
        },
    )


def _extract_place_display_name(place: dict[str, Any]) -> Optional[str]:
    display_name = place.get("displayName")
    if isinstance(display_name, dict):
        return _normalize_text(display_name.get("text"))
    return _normalize_text(display_name)


def search_nearby_kindergartens(
    session,
    *,
    radius_km: float = 10.0,
    bounds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not _google_places_api_available():
        result = {
            "provider_available": False,
            "provider_name": "google",
            "total": 0,
            "schools": [],
            "message": "尚未設定 GOOGLE_MAPS_API_KEY，無法查詢 Google Places API。",
        }
        if bounds:
            result["query_bounds"] = bounds
        return result

    campus_setting = get_or_create_campus_setting(session)
    campus = serialize_campus_setting(campus_setting)
    campus_lat = _safe_float(campus.get("campus_lat"))
    campus_lng = _safe_float(campus.get("campus_lng"))

    if campus_lat is None or campus_lng is None:
        return {
            "provider_available": True,
            "provider_name": "google",
            "total": 0,
            "schools": [],
            "message": "尚未設定本園座標，請先在設定中填入本園中心點。",
        }

    field_mask = (
        "places.id,"
        "places.displayName,"
        "places.formattedAddress,"
        "places.location,"
        "places.primaryType,"
        "places.types,"
        "places.businessStatus,"
        "places.googleMapsUri,"
        "places.rating,"
        "places.userRatingCount,"
        "nextPageToken"
    )

    schools_by_place_id: dict[str, dict[str, Any]] = {}

    # Google Places New Text Search API 只支援 "preschool" 作為 includedType。
    # "kindergarten" 在新版 API 無效（400），故改用 strictTypeFiltering=False，
    # 讓 Google 同時回傳「幼兒園」(preschool) 與「幼稚園」(kindergarten) 類型的學校。
    page_token: Optional[str] = None
    for _ in range(GOOGLE_PLACES_MAX_RESULTS // GOOGLE_PLACES_PAGE_SIZE):
        # 優先使用前端傳入的 bounding box，否則以本園座標 + 半徑計算
        if bounds:
            rect = {
                "low": {"latitude": bounds["south"], "longitude": bounds["west"]},
                "high": {"latitude": bounds["north"], "longitude": bounds["east"]},
            }
        else:
            rect = _radius_to_bounds(campus_lat, campus_lng, radius_km)

        payload: dict[str, Any] = {
            "textQuery": "幼兒園",
            "includedType": "preschool",
            "strictTypeFiltering": True,
            "languageCode": "zh-TW",
            "regionCode": "TW",
            "locationRestriction": {"rectangle": rect},
            "pageSize": GOOGLE_PLACES_PAGE_SIZE,
        }
        if page_token:
            payload["pageToken"] = page_token

        try:
            response = _query_google_places_text(payload, field_mask=field_mask)
        except Exception as exc:
            logger.warning(
                "Google Places Text Search 失敗 radius_km=%s err=%s",
                radius_km,
                exc,
            )
            break

        for place in response.get("places") or []:
            place_id = _normalize_text(place.get("id"))
            if not place_id or place_id in schools_by_place_id:
                continue

            location = place.get("location") or {}
            lat = _safe_float(location.get("latitude"))
            lng = _safe_float(location.get("longitude"))
            distance_km = None
            if None not in {campus_lat, campus_lng, lat, lng}:
                distance_km = round(_haversine_km(campus_lat, campus_lng, lat, lng), 2)

            raw_rating = place.get("rating")
            raw_rating_count = place.get("userRatingCount")
            schools_by_place_id[place_id] = {
                "place_id": place_id,
                "name": _extract_place_display_name(place),
                "formatted_address": _normalize_text(place.get("formattedAddress")),
                "lat": lat,
                "lng": lng,
                "primary_type": _normalize_text(place.get("primaryType")),
                "types": [
                    str(item).strip()
                    for item in (place.get("types") or [])
                    if str(item).strip()
                ],
                "business_status": _normalize_text(place.get("businessStatus")),
                "google_maps_uri": _normalize_text(place.get("googleMapsUri")),
                "distance_km": distance_km,
                "rating": float(raw_rating) if raw_rating is not None else None,
                "user_rating_count": (
                    int(raw_rating_count) if raw_rating_count is not None else None
                ),
            }

            if len(schools_by_place_id) >= GOOGLE_PLACES_MAX_RESULTS:
                break

        if len(schools_by_place_id) >= GOOGLE_PLACES_MAX_RESULTS:
            break

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    # ── Step 2：從 DB 取得高雄市所有在籍 MOE 幼兒園 ─────────────────────────
    moe_schools = (
        session.query(CompetitorSchool)
        .filter(
            CompetitorSchool.is_active == True,  # noqa: E712
            CompetitorSchool.city.like("%高雄%"),
        )
        .all()
    )

    # ── Step 3：將 Google 結果與 MOE DB 比對，寫入 enrichment 欄位 ──────────
    matched_moe_ids: set[int] = set()
    for school in schools_by_place_id.values():
        moe, score = _find_moe_match(
            school["name"],
            school.get("formatted_address", ""),
            moe_schools,
            google_place_id=school.get("place_id"),
            google_lat=school.get("lat"),
            google_lng=school.get("lng"),
        )
        if moe:
            matched_moe_ids.add(moe.id)
            school.update(_enrich_from_moe(moe))
            # 高信心比對 → 快取 place_id 以加速後續查詢
            if (
                score >= HIGH_CONFIDENCE
                and school.get("place_id")
                and not moe.google_place_id
            ):
                try:
                    moe.google_place_id = school["place_id"]
                    moe.google_name = school.get("name")
                    moe.google_rating = school.get("rating")
                    moe.google_rating_count = school.get("user_rating_count")
                    moe.google_maps_uri = school.get("google_maps_uri")
                    moe.google_matched_at = datetime.now()
                    moe.match_confidence = score
                    session.flush()
                except Exception:
                    logger.debug("快取 place_id 失敗：%s", school.get("name"))
        else:
            enrichment = _empty_enrichment(school.get("name", ""))
            school.update(enrichment)
            inferred = enrichment.get("school_type")
            logger.debug(
                "MOE 比對失敗：%s（%s）→ 推斷類型=%s",
                school.get("name"),
                school.get("formatted_address", "")[:30],
                inferred or "其他",
            )

    unmatched_count = len(schools_by_place_id) - len(matched_moe_ids)
    if unmatched_count > 0:
        logger.info(
            "幼兒園比對結果：%d 筆比對成功，%d 筆未比對到教育部資料",
            len(matched_moe_ids),
            unmatched_count,
        )

    # ── Step 4：MOE gap-fill — 補入 Google 未收錄但 DB 有記錄的學校 ─────────
    # 規則：已比對到 Google 結果的 MOE 學校略過；沒有座標的嘗試 geocode（有上限）。
    geocode_count = 0
    for moe in moe_schools:
        if moe.id in matched_moe_ids:
            continue

        lat, lng = _resolve_moe_coords(session, moe)

        if lat is None and geocode_count < MOE_GEOCODE_PER_REQUEST_LIMIT:
            lat, lng = _geocode_and_save_moe(session, moe)
            if lat is not None:
                geocode_count += 1

        if lat is None or lng is None:
            continue  # 無法取得座標，略過

        distance_km = round(_haversine_km(campus_lat, campus_lng, lat, lng), 2)
        if distance_km > radius_km:
            continue  # 超出半徑

        moe_key = f"moe:{moe.id}"
        schools_by_place_id[moe_key] = {
            "place_id": None,
            "name": moe.school_name,
            "formatted_address": moe.address or "",
            "lat": lat,
            "lng": lng,
            "primary_type": "preschool",
            "types": ["preschool"],
            "business_status": "OPERATIONAL" if moe.is_active else "CLOSED_TEMPORARILY",
            "google_maps_uri": None,
            "distance_km": distance_km,
            "rating": None,
            "user_rating_count": None,
            **_enrich_from_moe(moe),
            "source": "moe",  # 覆蓋 _enrich_from_moe 中的 "google"
        }
        logger.info("MOE gap-fill 加入：%s dist=%.2fkm", moe.school_name, distance_km)

    # ── Step 5：排序後回傳 ────────────────────────────────────────────────────
    schools = sorted(
        schools_by_place_id.values(),
        key=lambda item: (
            item["distance_km"] is None,
            item["distance_km"] if item["distance_km"] is not None else float("inf"),
            item["name"] or "",
        ),
    )
    result = {
        "provider_available": True,
        "provider_name": "google",
        "total": len(schools),
        "schools": schools,
        "message": None if schools else f"{radius_km} 公里內沒有幼兒園結果。",
    }
    if bounds:
        result["query_bounds"] = bounds
    return result


def _query_town_by_point(lat: float, lng: float) -> dict[str, Optional[str]]:
    try:
        xml_text = _request_text(f"{NLSC_TOWN_QUERY_URL}/{lng}/{lat}/4326")
        root = ET.fromstring(xml_text)
        return {
            "town_code": _extract_xml_text(root, ("townCode",)),
            "town_name": _extract_xml_text(root, ("townName",)),
            "county_name": _extract_xml_text(root, ("ctyName",)),
        }
    except Exception as exc:
        logger.warning(
            "NLSC TownVillagePointQuery 失敗 lat=%s lng=%s err=%s", lat, lng, exc
        )
        return {"town_code": None, "town_name": None, "county_name": None}


def _query_land_use(lat: float, lng: float) -> Optional[str]:
    try:
        xml_text = _request_text(f"{NLSC_LAND_USE_URL}/{lng}/{lat}/4326")
        root = ET.fromstring(xml_text)
        return _extract_xml_text(root, ("NAME", "Lname_C3"))
    except Exception as exc:
        logger.warning(
            "NLSC LandUsePointQuery 失敗 lat=%s lng=%s err=%s", lat, lng, exc
        )
        return None


def _resolve_population_density_url() -> list[str]:
    if RECRUITMENT_POPULATION_DENSITY_URL:
        return [RECRUITMENT_POPULATION_DENSITY_URL]

    roc_year = date.today().year - 1911
    return [
        f"https://www.ris.gov.tw/rs-opendata/api/v1/datastore/ODRP048/{year}"
        for year in range(roc_year - 1, max(roc_year - 4, 100), -1)
    ]


def load_population_density_index() -> dict[str, float]:
    for url in _resolve_population_density_url():
        try:
            payload = _request_json(url)
            records = _extract_records(payload)
            if not records:
                continue
            index: dict[str, float] = {}
            for row in records:
                site_id = _extract_first_value(row, _DISTRICT_KEYS)
                density = _extract_first_value(row, _DENSITY_KEYS)
                if not site_id or density in (None, ""):
                    continue
                numeric = _safe_float(density)
                if numeric is None:
                    continue
                index[_normalize_area_key(site_id)] = numeric
            if index:
                return index
        except Exception as exc:
            logger.warning("人口密度資料載入失敗 url=%s err=%s", url, exc)
    return {}


def load_population_age_index() -> dict[str, int]:
    if not RECRUITMENT_POPULATION_AGE_URL:
        return {}

    try:
        payload = _request_json(RECRUITMENT_POPULATION_AGE_URL)
    except Exception as exc:
        logger.warning(
            "幼齡人口資料載入失敗 url=%s err=%s", RECRUITMENT_POPULATION_AGE_URL, exc
        )
        return {}

    records = _extract_records(payload)
    if not records:
        return {}

    total_rows = []
    partial_rows = []
    for row in records:
        area = _extract_first_value(row, _DISTRICT_KEYS)
        if not area:
            continue
        sex = _extract_first_value(row, ("性別", "sex", "gender")) or ""
        total = 0
        for key in (
            "0歲人數",
            "1歲人數",
            "2歲人數",
            "3歲人數",
            "4歲人數",
            "5歲人數",
            "6歲人數",
        ):
            total += _safe_int(_extract_first_value(row, (key,))) or 0
        if not total:
            total = (
                (_safe_int(_extract_first_value(row, ("合計0至4歲人數",))) or 0)
                + (_safe_int(_extract_first_value(row, ("5歲人數",))) or 0)
                + (_safe_int(_extract_first_value(row, ("6歲人數",))) or 0)
            )
        if total <= 0:
            continue
        target = (
            total_rows
            if sex in {"計", "總計", "合計", "TOTAL", "Both", "全部"} or not sex
            else partial_rows
        )
        target.append((_normalize_area_key(area), total))

    source = total_rows or partial_rows
    age_index: dict[str, int] = {}
    for area_key, value in source:
        age_index[area_key] = age_index.get(area_key, 0) + value
    return age_index


def get_default_campus_payload() -> dict[str, Any]:
    return {
        "campus_name": RECRUITMENT_CAMPUS_NAME,
        "campus_address": RECRUITMENT_CAMPUS_ADDRESS,
        "campus_lat": _safe_float(RECRUITMENT_CAMPUS_LAT),
        "campus_lng": _safe_float(RECRUITMENT_CAMPUS_LNG),
        "travel_mode": (
            RECRUITMENT_CAMPUS_TRAVEL_MODE
            if RECRUITMENT_CAMPUS_TRAVEL_MODE in SUPPORTED_TRAVEL_MODES
            else "driving"
        ),
    }


def serialize_campus_setting(setting: RecruitmentCampusSetting) -> dict[str, Any]:
    return {
        "campus_name": setting.campus_name,
        "campus_address": setting.campus_address,
        "campus_lat": setting.campus_lat,
        "campus_lng": setting.campus_lng,
        "travel_mode": setting.travel_mode,
        "updated_at": setting.updated_at.isoformat() if setting.updated_at else None,
    }


def get_or_create_campus_setting(session) -> RecruitmentCampusSetting:
    setting = (
        session.query(RecruitmentCampusSetting)
        .order_by(RecruitmentCampusSetting.id.asc())
        .first()
    )
    if setting:
        return setting

    payload = get_default_campus_payload()
    setting = RecruitmentCampusSetting(**payload)
    session.add(setting)
    session.flush()
    return setting


def upsert_campus_setting(session, payload: dict[str, Any]) -> RecruitmentCampusSetting:
    setting = get_or_create_campus_setting(session)
    old_address = (setting.campus_address or "").strip()

    for field in ("campus_name", "campus_address", "travel_mode"):
        if field in payload and payload[field] is not None:
            setattr(setting, field, str(payload[field]).strip())

    # Explicit lat/lng from payload take priority
    explicit_lat = _safe_float(payload.get("campus_lat"))
    explicit_lng = _safe_float(payload.get("campus_lng"))
    setting.campus_lat = explicit_lat
    setting.campus_lng = explicit_lng

    if setting.travel_mode not in SUPPORTED_TRAVEL_MODES:
        setting.travel_mode = "driving"

    new_address = (setting.campus_address or "").strip()
    address_changed = new_address and new_address != old_address

    # Auto-geocode when: coordinates are missing, OR address was changed without supplying new coordinates
    needs_geocode = (
        (setting.campus_lat is None or setting.campus_lng is None)
        or (address_changed and explicit_lat is None and explicit_lng is None)
    ) and new_address
    if needs_geocode:
        metadata = resolve_address_metadata(
            new_address,
            campus=serialize_campus_setting(setting),
            include_land_use=False,
        )
        if metadata.get("lat") is not None and metadata.get("lng") is not None:
            setting.campus_lat = metadata["lat"]
            setting.campus_lng = metadata["lng"]

    setting.updated_at = datetime.now()
    session.flush()
    return setting


def resolve_address_metadata(
    address: str,
    *,
    campus: Optional[dict[str, Any]] = None,
    include_land_use: bool = True,
) -> dict[str, Any]:
    normalized_address = _normalize_text(address)
    metadata: dict[str, Any] = {
        "formatted_address": normalized_address,
        "matched_address": normalized_address,
        "lat": None,
        "lng": None,
        "google_place_id": None,
        "provider": None,
        "town_code": None,
        "town_name": None,
        "county_name": None,
        "land_use_label": None,
        "travel_distance_km": None,
        "travel_minutes": None,
        "data_quality": "partial",
    }
    if not normalized_address:
        return metadata

    preferred_result = None
    fallback_result = None

    try:
        if _google_available():
            preferred_result = _query_google_address(normalized_address)
        elif _tgos_available():
            preferred_result = _query_tgos_address(normalized_address)
    except Exception as exc:
        logger.warning(
            "主要地址定位失敗 address=%s provider=%s err=%s",
            normalized_address,
            current_market_provider(),
            exc,
        )

    if (
        preferred_result
        and preferred_result.get("lat") is not None
        and preferred_result.get("lng") is not None
    ):
        metadata.update(
            {
                "lat": preferred_result["lat"],
                "lng": preferred_result["lng"],
                "formatted_address": preferred_result.get("formatted_address")
                or preferred_result["matched_address"],
                "matched_address": preferred_result["matched_address"],
                "google_place_id": preferred_result.get("google_place_id"),
                "provider": preferred_result["provider"],
            }
        )
    else:
        try:
            if _google_available() and _tgos_available():
                fallback_result = _query_tgos_address(normalized_address)
            elif not _google_available():
                fallback_result = geocode_address(normalized_address)
            elif current_geocoding_provider() not in {None, "google"}:
                fallback_result = geocode_address(normalized_address)
        except Exception as exc:
            logger.warning(
                "備援地址定位失敗 address=%s err=%s", normalized_address, exc
            )

        if fallback_result:
            metadata.update(
                {
                    "lat": fallback_result["lat"],
                    "lng": fallback_result["lng"],
                    "formatted_address": (
                        fallback_result.get("formatted_address")
                        or fallback_result.get("matched_address")
                        or normalized_address
                    ),
                    "matched_address": (
                        fallback_result.get("matched_address")
                        or fallback_result.get("formatted_address")
                        or normalized_address
                    ),
                    "google_place_id": fallback_result.get("google_place_id"),
                    "provider": fallback_result.get("provider")
                    or current_market_provider(),
                    "data_quality": "estimated",
                }
            )

    if metadata["lat"] is not None and metadata["lng"] is not None:
        town_meta = _query_town_by_point(metadata["lat"], metadata["lng"])
        metadata.update(town_meta)
        if include_land_use:
            metadata["land_use_label"] = _query_land_use(
                metadata["lat"], metadata["lng"]
            )

    county_name, district_name = _extract_county_district(
        metadata["matched_address"] or normalized_address
    )
    if not metadata["county_name"]:
        metadata["county_name"] = county_name
    if not metadata["town_name"]:
        metadata["town_name"] = district_name

    if (
        campus
        and campus.get("campus_lat") is not None
        and campus.get("campus_lng") is not None
        and metadata["lat"] is not None
        and metadata["lng"] is not None
    ):
        route_distance = None
        route_minutes = None
        route_quality = "partial"
        if _google_available():
            route_distance, route_minutes, route_quality = _query_google_route(
                campus_point=campus,
                target_point=metadata,
            )
        if route_distance is None and route_minutes is None and _tgos_available():
            route_distance, route_minutes, route_quality = _query_tgos_route(
                campus_point=campus,
                target_point=metadata,
            )

        if route_distance is not None or route_minutes is not None:
            metadata["travel_distance_km"] = route_distance
            metadata["travel_minutes"] = route_minutes
            metadata["data_quality"] = route_quality
        else:
            est_distance, est_minutes, est_quality = _estimate_travel(
                _haversine_km(
                    campus["campus_lat"],
                    campus["campus_lng"],
                    metadata["lat"],
                    metadata["lng"],
                ),
                campus.get("travel_mode") or "driving",
            )
            metadata["travel_distance_km"] = est_distance
            metadata["travel_minutes"] = est_minutes
            if metadata["data_quality"] != "estimated":
                metadata["data_quality"] = est_quality

    if (
        metadata["provider"] in {"google", "tgos"}
        and metadata["data_quality"] != "estimated"
        and metadata["town_code"]
        and metadata["land_use_label"]
        and metadata["travel_minutes"] is not None
    ):
        metadata["data_quality"] = "complete"
    elif (
        metadata["provider"] in {"google", "tgos"}
        and metadata["data_quality"] != "estimated"
        and metadata["town_code"]
    ):
        metadata["data_quality"] = "partial"

    return metadata


def _infer_target_county(session, campus: dict[str, Any]) -> Optional[str]:
    if campus.get("campus_address"):
        county_name, _district = _extract_county_district(campus["campus_address"])
        if county_name:
            return county_name

    visit = (
        session.query(RecruitmentVisit)
        .filter(
            RecruitmentVisit.address.isnot(None),
            RecruitmentVisit.address != "",
        )
        .order_by(RecruitmentVisit.created_at.desc())
        .first()
    )
    if visit and visit.address:
        county_name, _district = _extract_county_district(visit.address)
        if county_name:
            return county_name

    district_visit = (
        session.query(
            RecruitmentVisit.district, func.count(RecruitmentVisit.id).label("cnt")
        )
        .filter(
            RecruitmentVisit.district.isnot(None),
            RecruitmentVisit.district != "",
        )
        .group_by(RecruitmentVisit.district)
        .order_by(func.count(RecruitmentVisit.id).desc())
        .first()
    )
    if district_visit and district_visit[0]:
        return "高雄市"
    return None


def _resolve_data_completeness(*, has_density: bool, has_population_age: bool) -> str:
    if has_density and has_population_age:
        return "complete"
    if has_density or has_population_age:
        return "partial"
    return "cached"


def is_google_stale_cache_row(row: Optional[RecruitmentGeocodeCache]) -> bool:
    if not row:
        return False
    if row.status == "failed":
        return True
    if row.status != "resolved":
        return False
    return (row.provider or "").lower() != "google"


def _apply_metadata_to_geocode_cache(
    row: RecruitmentGeocodeCache,
    metadata: dict[str, Any],
    *,
    district: Optional[str] = None,
    error_message: str = "geocoding failed",
) -> None:
    row.district = district or row.district or metadata.get("town_name")
    row.provider = metadata.get("provider")
    row.formatted_address = (
        metadata.get("formatted_address")
        or metadata.get("matched_address")
        or row.address
    )
    row.matched_address = (
        metadata.get("matched_address")
        or metadata.get("formatted_address")
        or row.address
    )
    row.google_place_id = metadata.get("google_place_id")
    row.lat = metadata.get("lat")
    row.lng = metadata.get("lng")
    row.town_code = metadata.get("town_code")
    row.town_name = metadata.get("town_name")
    row.county_name = metadata.get("county_name")
    row.land_use_label = metadata.get("land_use_label")
    row.travel_minutes = metadata.get("travel_minutes")
    row.travel_distance_km = metadata.get("travel_distance_km")
    row.data_quality = metadata.get("data_quality") or "partial"
    row.updated_at = datetime.now()

    if metadata.get("lat") is not None and metadata.get("lng") is not None:
        row.status = "resolved"
        row.error_message = None
        row.resolved_at = datetime.now()
    else:
        row.status = "failed"
        row.error_message = error_message


def _group_hotspots_by_district(
    session,
    addresses: Optional[list[str]] = None,
) -> dict[str, list[RecruitmentGeocodeCache]]:
    grouped: dict[str, list[RecruitmentGeocodeCache]] = {}
    query = session.query(RecruitmentGeocodeCache).filter(
        RecruitmentGeocodeCache.district.isnot(None),
        RecruitmentGeocodeCache.travel_minutes.isnot(None),
    )
    if addresses is not None:
        if not addresses:
            return grouped
        query = query.filter(RecruitmentGeocodeCache.address.in_(addresses))
    rows = query.all()
    for row in rows:
        grouped.setdefault(row.district, []).append(row)
    return grouped


def _preferred_hotspot_row_by_district(
    session,
    addresses: Optional[list[str]] = None,
) -> dict[str, RecruitmentGeocodeCache]:
    query = session.query(RecruitmentGeocodeCache).filter(
        RecruitmentGeocodeCache.district.isnot(None)
    )
    if addresses is not None:
        if not addresses:
            return {}
        query = query.filter(RecruitmentGeocodeCache.address.in_(addresses))
    rows = query.order_by(RecruitmentGeocodeCache.updated_at.desc()).all()
    selected: dict[str, RecruitmentGeocodeCache] = {}
    for row in rows:
        current = selected.get(row.district)
        if current is None:
            selected[row.district] = row
            continue
        current_is_google = (current.provider or "").lower() == "google"
        row_is_google = (row.provider or "").lower() == "google"
        if row_is_google and not current_is_google:
            selected[row.district] = row
    return selected


def _average_travel_minutes(rows: list[RecruitmentGeocodeCache]) -> Optional[float]:
    if not rows:
        return None

    google_rows = [row for row in rows if (row.provider or "").lower() == "google"]
    source_rows = google_rows or rows
    minutes = [
        row.travel_minutes for row in source_rows if row.travel_minutes is not None
    ]
    if not minutes:
        return None
    return round(sum(minutes) / len(minutes), 1)


def sync_market_intelligence(session, *, hotspot_limit: int = 200) -> dict[str, Any]:
    campus_setting = get_or_create_campus_setting(session)
    campus = serialize_campus_setting(campus_setting)
    target_county = _infer_target_county(session, campus)

    hotspot_rows = (
        session.query(RecruitmentGeocodeCache)
        .filter(
            RecruitmentGeocodeCache.address.isnot(None),
            RecruitmentGeocodeCache.address != "",
        )
        .order_by(RecruitmentGeocodeCache.updated_at.desc())
        .limit(hotspot_limit)
        .all()
    )

    if not hotspot_rows:
        hotspot_addresses = [
            row[0]
            for row in session.query(RecruitmentVisit.address)
            .filter(
                RecruitmentVisit.address.isnot(None), RecruitmentVisit.address != ""
            )
            .distinct()
            .limit(hotspot_limit)
            .all()
        ]
        hotspot_rows = []
        for address in hotspot_addresses:
            row = (
                session.query(RecruitmentGeocodeCache)
                .filter_by(address=address)
                .first()
            )
            if not row:
                row = RecruitmentGeocodeCache(address=address)
                session.add(row)
                session.flush()
            hotspot_rows.append(row)

    enriched_hotspots = 0
    districts_in_use: set[str] = set()
    for row in hotspot_rows:
        metadata = resolve_address_metadata(row.address, campus=campus)
        _apply_metadata_to_geocode_cache(
            row,
            metadata,
            district=row.district or metadata.get("town_name"),
            error_message="market intelligence resolve failed",
        )
        if row.district:
            districts_in_use.add(row.district)
        enriched_hotspots += 1

    density_index = load_population_density_index()
    age_index = load_population_age_index()
    sync_warning = None

    for district in sorted(districts_in_use):
        row = (
            session.query(RecruitmentAreaInsightCache)
            .filter_by(district=district)
            .first()
        )
        if not row:
            row = RecruitmentAreaInsightCache(district=district)
            session.add(row)

        related_hotspots = [item for item in hotspot_rows if item.district == district]
        town_code = next(
            (item.town_code for item in related_hotspots if item.town_code), None
        )
        county_name = next(
            (item.county_name for item in related_hotspots if item.county_name),
            target_county,
        )
        density_key_candidates = [
            _normalize_area_key(f"{county_name or ''}{district}"),
            _normalize_area_key(district),
        ]
        population_density = next(
            (
                density_index[key]
                for key in density_key_candidates
                if key in density_index
            ),
            None,
        )
        population_0_6 = next(
            (age_index[key] for key in density_key_candidates if key in age_index), None
        )

        row.county_name = county_name
        row.town_code = town_code
        row.population_density = population_density
        row.population_0_6 = population_0_6
        row.data_completeness = _resolve_data_completeness(
            has_density=population_density is not None,
            has_population_age=population_0_6 is not None,
        )
        row.source_notes = "density:ris; age:optional; travel:geocoding_cache"
        row.synced_at = datetime.now()
        row.updated_at = datetime.now()

    session.flush()
    return {
        "campus": campus,
        "target_county": target_county,
        "hotspots_synced": enriched_hotspots,
        "area_rows": session.query(RecruitmentAreaInsightCache).count(),
        "warning": sync_warning,
        "synced_at": datetime.now().isoformat(),
    }


def _district_lead_metrics(
    session, dataset_scope: Optional[str] = None
) -> dict[str, dict[str, Any]]:
    threshold_30 = datetime.now() - timedelta(days=30)
    threshold_90 = datetime.now() - timedelta(days=90)

    metrics: dict[str, dict[str, Any]] = {}
    visits = _scoped_visit_query(session, dataset_scope).all()
    for visit in visits:
        district = (
            visit.district or _extract_county_district(visit.address)[1] or "未填寫"
        )
        bucket = metrics.setdefault(
            district,
            {
                "district": district,
                "lead_count_30d": 0,
                "lead_count_90d": 0,
                "deposit_90d": 0,
                "visit_90d": 0,
            },
        )
        created_at = visit.created_at or datetime.now()
        if created_at >= threshold_30:
            bucket["lead_count_30d"] += 1
        if created_at >= threshold_90:
            bucket["lead_count_90d"] += 1
            bucket["visit_90d"] += 1
            bucket["deposit_90d"] += 1 if visit.has_deposit else 0
    return metrics


def _build_market_district_rows(
    session, dataset_scope: Optional[str] = None
) -> list[dict[str, Any]]:
    lead_metrics = _district_lead_metrics(session, dataset_scope=dataset_scope)
    scoped_addresses = _scoped_hotspot_addresses(session, dataset_scope=dataset_scope)
    hotspot_rows = _preferred_hotspot_row_by_district(session, scoped_addresses)
    district_travel_rows = _group_hotspots_by_district(session, scoped_addresses)
    area_rows = {
        row.district: row for row in session.query(RecruitmentAreaInsightCache).all()
    }

    if _normalize_dataset_scope(dataset_scope) == DATASET_SCOPE_ALL:
        districts = sorted(set(lead_metrics) | set(area_rows) | set(hotspot_rows))
    else:
        districts = sorted(set(lead_metrics) | set(hotspot_rows))
    rows: list[dict[str, Any]] = []
    for district in districts:
        metrics = lead_metrics.get(district, {})
        area_row = area_rows.get(district)
        hotspot = hotspot_rows.get(district)
        visit_90d = metrics.get("visit_90d", 0)
        deposit_90d = metrics.get("deposit_90d", 0)
        rows.append(
            {
                "district": district,
                "town_code": (hotspot.town_code if hotspot else None)
                or (area_row.town_code if area_row else None),
                "lead_count_30d": metrics.get("lead_count_30d", 0),
                "lead_count_90d": metrics.get("lead_count_90d", 0),
                "deposit_rate_90d": (
                    round((deposit_90d / visit_90d) * 100, 1) if visit_90d else 0.0
                ),
                "avg_travel_minutes": _average_travel_minutes(
                    district_travel_rows.get(district, [])
                ),
                "population_density": area_row.population_density if area_row else None,
                "population_0_6": area_row.population_0_6 if area_row else None,
                "data_completeness": (
                    area_row.data_completeness if area_row else "partial"
                ),
            }
        )
    return sorted(rows, key=lambda item: (-item["lead_count_90d"], item["district"]))


def build_market_intelligence_snapshot(
    session, dataset_scope: Optional[str] = None
) -> dict[str, Any]:
    campus_setting = get_or_create_campus_setting(session)
    campus = serialize_campus_setting(campus_setting)
    districts = _build_market_district_rows(session, dataset_scope=dataset_scope)

    synced_at = session.query(func.max(RecruitmentAreaInsightCache.synced_at)).scalar()
    dataset_quality = "partial"
    if districts:
        completeness = {item["data_completeness"] for item in districts}
        if completeness == {"complete"}:
            dataset_quality = "complete"
        elif completeness == {"cached"}:
            dataset_quality = "cached"

    return {
        "campus": campus,
        "districts": districts,
        "data_completeness": dataset_quality,
        "synced_at": synced_at.isoformat() if synced_at else None,
    }
