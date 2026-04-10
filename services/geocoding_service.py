"""
services/geocoding_service.py — 招生地址 geocoding provider abstraction
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_GEOCODING_TIMEOUT = float(os.environ.get("GEOCODING_TIMEOUT_SECONDS", "8"))
_GOOGLE_GEOCODING_URL = os.environ.get(
    "GOOGLE_GEOCODING_URL",
    "https://maps.googleapis.com/maps/api/geocode/json",
)
_NOMINATIM_GEOCODING_URL = os.environ.get(
    "GEOCODING_NOMINATIM_URL",
    "https://nominatim.openstreetmap.org/search",
)
_GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
_GEOCODING_PROVIDER = os.environ.get("GEOCODING_PROVIDER", "").strip().lower()
_GEOCODING_USER_AGENT = os.environ.get(
    "GEOCODING_USER_AGENT",
    "ivyManageSystem/1.0 (+https://example.invalid)",
).strip()
_GEOCODING_CONTACT_EMAIL = os.environ.get("GEOCODING_CONTACT_EMAIL", "").strip()

_nominatim_lock = threading.Lock()
_last_nominatim_request_at = 0.0


def _resolve_provider() -> Optional[str]:
    if _GEOCODING_PROVIDER in {"disabled", "off", "none"}:
        return None
    if _GEOCODING_PROVIDER in {"google", "nominatim"}:
        return _GEOCODING_PROVIDER
    if _GOOGLE_MAPS_API_KEY:
        return "google"
    return "nominatim"


def current_geocoding_provider() -> Optional[str]:
    return _resolve_provider()


def can_geocode() -> bool:
    return current_geocoding_provider() is not None


def _normalize_query_address(address: str) -> str:
    normalized = " ".join((address or "").strip().split())
    normalized = (
        normalized
        .replace("台灣", "臺灣")
        .replace("台北", "臺北")
        .replace("台中", "臺中")
        .replace("台南", "臺南")
        .replace("台東", "臺東")
    )
    if normalized and "臺灣" not in normalized and "台灣" not in normalized:
        normalized = f"臺灣 {normalized}"
    return normalized


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = " ".join((value or "").split()).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _strip_floor_suffix(address: str) -> str:
    stripped = re.sub(r'(?:[Bb]\d+|地下\d+樓|\d+樓(?:之\d+)?|之\d+)$', '', address).strip()
    stripped = re.sub(r'[之\-]\d+$', '', stripped).strip()
    return stripped


def _extract_address_parts(address: str) -> Optional[tuple[str, str, str]]:
    base = address.removeprefix("臺灣 ").strip()
    matched = re.match(r'(?P<city>.+?[縣市])(?P<district>.+?(?:區|鄉|鎮|市))(?P<detail>.+)', base)
    if not matched:
        return None
    city = matched.group('city').strip()
    district = matched.group('district').strip()
    detail = matched.group('detail').strip()
    if not city or not district or not detail:
        return None
    return city, district, detail


def _simplify_road_segment(detail: str) -> str:
    simplified = _strip_floor_suffix(detail)
    simplified = re.sub(r'(?:\d+號.*)$', '', simplified).strip()
    simplified = re.sub(r'(?:\d+弄.*)$', '', simplified).strip()
    simplified = re.sub(r'(?:\d+巷.*)$', '', simplified).strip()
    return simplified


def _reorder_detail_for_nominatim(detail: str) -> str:
    reordered = _strip_floor_suffix(detail)
    reordered = re.sub(r'([路街道段巷弄])(\d)', r'\1 \2', reordered)
    return reordered.strip()


def _build_nominatim_query_candidates(address: str) -> list[str]:
    normalized = _normalize_query_address(address)
    base = normalized.removeprefix("臺灣 ").strip()
    candidates = [normalized, base]

    parts = _extract_address_parts(normalized)
    if not parts:
        return _dedupe_keep_order(candidates)

    city, district, detail = parts
    detail_no_floor = _strip_floor_suffix(detail)
    detail_reordered = _reorder_detail_for_nominatim(detail)
    detail_no_floor_reordered = _reorder_detail_for_nominatim(detail_no_floor)
    road_only = _simplify_road_segment(detail_no_floor)
    district_scope = f"{district} {city}"

    candidates.extend([
        f"{detail} {district} {city}",
        f"{detail_reordered} {district} {city}",
        f"{detail_no_floor} {district} {city}",
        f"{detail_no_floor_reordered} {district} {city}",
        f"{city}{district}{detail_no_floor}",
        f"{road_only} {district} {city}",
        f"{city}{district}{road_only}",
        district_scope,
        f"{city}{district}",
    ])
    return _dedupe_keep_order(candidates)


def _throttle_nominatim() -> None:
    global _last_nominatim_request_at
    with _nominatim_lock:
        elapsed = time.monotonic() - _last_nominatim_request_at
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        _last_nominatim_request_at = time.monotonic()


def _geocode_with_google(address: str) -> Optional[dict]:
    if not _GOOGLE_MAPS_API_KEY:
        return None

    resp = requests.get(
        _GOOGLE_GEOCODING_URL,
        params={
            "address": _normalize_query_address(address),
            "key": _GOOGLE_MAPS_API_KEY,
            "language": "zh-TW",
            "region": "tw",
        },
        timeout=_GEOCODING_TIMEOUT,
    )
    resp.raise_for_status()

    payload = resp.json()
    results = payload.get("results") or []
    if payload.get("status") != "OK" or not results:
        logger.warning("Google geocoding 無結果: status=%s address=%s", payload.get("status"), address)
        return None

    top = results[0]
    location = top.get("geometry", {}).get("location", {})
    lat = location.get("lat")
    lng = location.get("lng")
    if lat is None or lng is None:
        return None

    return {
        "provider": "google",
        "lat": float(lat),
        "lng": float(lng),
        "formatted_address": top.get("formatted_address") or address,
    }


def _geocode_with_nominatim(address: str) -> Optional[dict]:
    for query in _build_nominatim_query_candidates(address):
        _throttle_nominatim()

        params = {
            "q": query,
            "format": "jsonv2",
            "limit": 1,
            "addressdetails": 1,
            "countrycodes": "tw",
        }
        if _GEOCODING_CONTACT_EMAIL:
            params["email"] = _GEOCODING_CONTACT_EMAIL

        resp = requests.get(
            _NOMINATIM_GEOCODING_URL,
            params=params,
            headers={"User-Agent": _GEOCODING_USER_AGENT},
            timeout=_GEOCODING_TIMEOUT,
        )
        resp.raise_for_status()

        results = resp.json() or []
        if not results:
            continue

        top = results[0]
        lat = top.get("lat")
        lng = top.get("lon")
        if lat is None or lng is None:
            continue

        return {
            "provider": "nominatim",
            "lat": float(lat),
            "lng": float(lng),
            "formatted_address": top.get("display_name") or address,
        }

    logger.warning("Nominatim geocoding 無結果: address=%s", address)
    return None


def geocode_address(address: str) -> Optional[dict]:
    provider = current_geocoding_provider()
    if not provider:
        return None

    try:
        if provider == "google":
            return _geocode_with_google(address)
        return _geocode_with_nominatim(address)
    except Exception as exc:
        logger.warning("geocode_address 失敗 provider=%s address=%s err=%s", provider, address, exc)
        return None
