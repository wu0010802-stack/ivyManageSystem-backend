"""義華校官網後台同步服務。"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from models.base import session_scope
from models.recruitment import RecruitmentSyncState, RecruitmentVisit

logger = logging.getLogger(__name__)

IVYKIDS_BACKEND_SOURCE = "ivykids_yihua_backend"
IVYKIDS_PROVIDER_LABEL = "義華校官網"
DEFAULT_LOGIN_URL = "https://www.ivykids.tw/manage/"
DEFAULT_DATA_URL = "https://www.ivykids.tw/manage/make_an_appointment/"
DEFAULT_SYNC_INTERVAL_MINUTES = 10
MAX_SYNC_PAGES = 20
REQUEST_TIMEOUT_SECONDS = 20
PREVIEW_LIMIT = 8

_SYNC_LOCK = threading.Lock()
_DATE_PATTERNS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y.%m.%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y.%m.%d %H:%M:%S",
)
_TEXT_EMPTY_MARKERS = {"", "-", "--", "未填寫", "無", "n/a", "na", "none"}
_FIELD_KEY_CLEANER = re.compile(r"[\s:：_\-()/]+")
_TAG_RE = re.compile(r"(?is)<[^>]+>")
_BLOCK_TAG_RE = re.compile(r"(?is)</?(?:br|p|div|tr|td|th|li|label|option|section|article|h\d)[^>]*>")
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)\b.*?>.*?</\1>")
_TAG_ATTR_RE = re.compile(
    r"([A-Za-z_:][\w:.-]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'=<>`]+))"
)
_LIST_ROW_RE = re.compile(r"(?is)<tr\b[^>]*>(.*?)</tr>")
_CELL_RE = re.compile(r"(?is)<(?:td|th)\b[^>]*>(.*?)</(?:td|th)>")
_INPUT_RE = re.compile(r"(?is)<input\b([^>]*)>")
_TEXTAREA_RE = re.compile(r"(?is)<textarea\b([^>]*)>(.*?)</textarea>")
_SELECT_RE = re.compile(r"(?is)<select\b([^>]*)>(.*?)</select>")
_OPTION_RE = re.compile(r"(?is)<option\b([^>]*)>(.*?)</option>")
_LABEL_RE = re.compile(r"(?is)<label\b([^>]*)>(.*?)</label>")
_NEXT_PAGE_RE = re.compile(r"(?is)<a\b[^>]*href=(?:\"([^\"]+)\"|'([^']+)')[^>]*>")
_DETAIL_LINK_RE = re.compile(r"id=(\d+)")
_FOUR_DIGIT_DATE_RE = re.compile(r"(?<!\d)(\d{4})[./-](\d{1,2})[./-](\d{1,2})(?!\d)")
_ROC_DATE_RE = re.compile(r"(?<!\d)(\d{3})[./-](\d{1,2})[./-](\d{1,2})(?!\d)")
_BOOL_TRUE_TOKENS = ("是", "有", "已", "true", "yes", "y", "1")
_BOOL_FALSE_TOKENS = ("否", "無", "未", "false", "no", "n", "0")


@dataclass(frozen=True)
class IvykidsBackendRecord:
    external_id: str
    status: Optional[str]
    visit_date: Optional[str]
    child_name: Optional[str]
    phone: Optional[str]
    source: Optional[str]
    created_at: Optional[str]
    detail_url: Optional[str]
    month: Optional[str] = None
    birthday: Optional[date] = None
    grade: Optional[str] = None
    address: Optional[str] = None
    district: Optional[str] = None
    referrer: Optional[str] = None
    notes: Optional[str] = None
    parent_response: Optional[str] = None
    deposit_collector: Optional[str] = None
    has_deposit: Optional[bool] = None
    enrolled: Optional[bool] = None
    transfer_term: Optional[bool] = None


def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return default
    stripped = value.strip()
    return stripped or default


def _get_login_url() -> str:
    return _get_env("IVYKIDS_LOGIN_URL", DEFAULT_LOGIN_URL) or DEFAULT_LOGIN_URL


def _get_data_url() -> str:
    return _get_env("IVYKIDS_DATA_URL", DEFAULT_DATA_URL) or DEFAULT_DATA_URL


IVYKIDS_LOGIN_URL = _get_login_url()
IVYKIDS_DATA_URL = _get_data_url()


def _get_credentials() -> tuple[str, str]:
    return (
        _get_env("IVYKIDS_USERNAME", "") or "",
        _get_env("IVYKIDS_PASSWORD", "") or "",
    )


def sync_configured() -> bool:
    username, password = _get_credentials()
    return bool(username and password)


def scheduler_requested() -> bool:
    flag = (_get_env("IVYKIDS_SYNC_ENABLED", "false") or "false").lower()
    return flag in {"1", "true", "yes", "on"}


def scheduler_configured() -> bool:
    return scheduler_requested() and sync_configured()


def get_sync_interval_minutes() -> int:
    raw = _get_env("IVYKIDS_SYNC_INTERVAL_MINUTES", str(DEFAULT_SYNC_INTERVAL_MINUTES))
    try:
        return max(1, int(raw or DEFAULT_SYNC_INTERVAL_MINUTES))
    except (TypeError, ValueError):
        return DEFAULT_SYNC_INTERVAL_MINUTES


def _build_requests_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        ),
    })
    return session


def _normalize_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = html.unescape(str(value)).replace("\xa0", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return None if text.lower() in _TEXT_EMPTY_MARKERS else text


def _strip_tags(fragment: Optional[str]) -> Optional[str]:
    if not fragment:
        return None
    cleaned = _SCRIPT_STYLE_RE.sub(" ", fragment)
    cleaned = _BLOCK_TAG_RE.sub(" ", cleaned)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return _normalize_text(cleaned)


def _parse_attrs(fragment: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for key, dq_value, sq_value, bare_value in _TAG_ATTR_RE.findall(fragment):
        value = dq_value or sq_value or bare_value or ""
        attrs[key.lower()] = html.unescape(value)
    lowered = fragment.lower()
    for boolean_key in ("checked", "selected", "disabled"):
        if re.search(rf"\b{boolean_key}\b", lowered):
            attrs[boolean_key] = boolean_key
    return attrs


def _normalize_field_key(value: Optional[str]) -> Optional[str]:
    text = _normalize_text(value)
    if not text:
        return None
    return _FIELD_KEY_CLEANER.sub("", text).lower()


def _parse_bool(value: Optional[str]) -> Optional[bool]:
    text = _normalize_text(value)
    if text is None:
        return None
    lowered = text.lower()
    if any(token == lowered or token in text for token in _BOOL_FALSE_TOKENS):
        return False
    if any(token == lowered or token in text for token in _BOOL_TRUE_TOKENS):
        return True
    return None


def _parse_date_value(value: Optional[str]) -> Optional[date]:
    text = _normalize_text(value)
    if not text:
        return None

    normalized = (
        text.replace("年", ".")
        .replace("月", ".")
        .replace("日", "")
        .replace("/", ".")
        .replace("-", ".")
    )

    western_match = _FOUR_DIGIT_DATE_RE.search(normalized)
    if western_match:
        try:
            return date(
                int(western_match.group(1)),
                int(western_match.group(2)),
                int(western_match.group(3)),
            )
        except ValueError:
            return None

    roc_match = _ROC_DATE_RE.search(normalized)
    if roc_match:
        try:
            return date(
                int(roc_match.group(1)) + 1911,
                int(roc_match.group(2)),
                int(roc_match.group(3)),
            )
        except ValueError:
            return None

    for pattern in _DATE_PATTERNS:
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _parse_month_from_value(value: Optional[str]) -> Optional[str]:
    dt = _parse_date_value(value)
    if not dt:
        return None
    return f"{dt.year - 1911}.{dt.month:02d}"


def _normalize_signature_date(value: Optional[str]) -> Optional[str]:
    dt = _parse_date_value(value)
    if dt:
        return dt.isoformat()

    text = _normalize_text(value)
    if not text:
        return None

    western_match = _FOUR_DIGIT_DATE_RE.search(text)
    if western_match:
        try:
            dt = date(
                int(western_match.group(1)),
                int(western_match.group(2)),
                int(western_match.group(3)),
            )
            return dt.isoformat()
        except ValueError:
            return None

    roc_match = _ROC_DATE_RE.search(text)
    if roc_match:
        try:
            dt = date(
                int(roc_match.group(1)) + 1911,
                int(roc_match.group(2)),
                int(roc_match.group(3)),
            )
            return dt.isoformat()
        except ValueError:
            return None

    return None


def _extract_control_values(fragment: str) -> list[str]:
    values: list[str] = []

    for attrs_fragment in _INPUT_RE.findall(fragment):
        attrs = _parse_attrs(attrs_fragment)
        input_type = (attrs.get("type") or "text").lower()
        if input_type in {"checkbox", "radio"} and "checked" not in attrs:
            continue
        raw_value = attrs.get("value")
        if input_type in {"checkbox", "radio"} and raw_value is None:
            raw_value = "true"
        text = _normalize_text(raw_value)
        if text:
            values.append(text)

    for attrs_fragment, content in _TEXTAREA_RE.findall(fragment):
        del attrs_fragment
        text = _strip_tags(content)
        if text:
            values.append(text)

    for attrs_fragment, content in _SELECT_RE.findall(fragment):
        del attrs_fragment
        options = _OPTION_RE.findall(content)
        selected_value = None
        for option_attrs_fragment, option_content in options:
            option_attrs = _parse_attrs(option_attrs_fragment)
            option_text = _strip_tags(option_content)
            option_value = _normalize_text(option_attrs.get("value")) or option_text
            if "selected" in option_attrs:
                selected_value = option_value
                break
            if selected_value is None and option_value:
                selected_value = option_value
        if selected_value:
            values.append(selected_value)

    return values


def _extract_named_detail_fields(page_html: str) -> dict[str, str]:
    field_map: dict[str, str] = {}
    id_value_map: dict[str, str] = {}

    for attrs_fragment in _INPUT_RE.findall(page_html):
        attrs = _parse_attrs(attrs_fragment)
        input_type = (attrs.get("type") or "text").lower()
        if input_type in {"checkbox", "radio"} and "checked" not in attrs:
            continue
        raw_value = attrs.get("value")
        if input_type in {"checkbox", "radio"} and raw_value is None:
            raw_value = "true"
        value = _normalize_text(raw_value)
        for key_name in ("name", "id"):
            key = _normalize_field_key(attrs.get(key_name))
            if key and value and key not in field_map:
                field_map[key] = value
        element_id = _normalize_field_key(attrs.get("id"))
        if element_id and value:
            id_value_map[element_id] = value

    for attrs_fragment, content in _TEXTAREA_RE.findall(page_html):
        attrs = _parse_attrs(attrs_fragment)
        value = _strip_tags(content)
        if not value:
            continue
        for key_name in ("name", "id"):
            key = _normalize_field_key(attrs.get(key_name))
            if key and key not in field_map:
                field_map[key] = value
        element_id = _normalize_field_key(attrs.get("id"))
        if element_id:
            id_value_map[element_id] = value

    for attrs_fragment, content in _SELECT_RE.findall(page_html):
        attrs = _parse_attrs(attrs_fragment)
        selected_value = None
        for option_attrs_fragment, option_content in _OPTION_RE.findall(content):
            option_attrs = _parse_attrs(option_attrs_fragment)
            option_text = _strip_tags(option_content)
            option_value = _normalize_text(option_attrs.get("value")) or option_text
            if "selected" in option_attrs:
                selected_value = option_value
                break
            if selected_value is None and option_value:
                selected_value = option_value
        if not selected_value:
            continue
        for key_name in ("name", "id"):
            key = _normalize_field_key(attrs.get(key_name))
            if key and key not in field_map:
                field_map[key] = selected_value
        element_id = _normalize_field_key(attrs.get("id"))
        if element_id:
            id_value_map[element_id] = selected_value

    for attrs_fragment, content in _LABEL_RE.findall(page_html):
        attrs = _parse_attrs(attrs_fragment)
        label_text = _strip_tags(content)
        target = _normalize_field_key(attrs.get("for"))
        if target and label_text and target in id_value_map:
            label_key = _normalize_field_key(label_text)
            if label_key and label_key not in field_map:
                field_map[label_key] = id_value_map[target]

    for row_html in _LIST_ROW_RE.findall(page_html):
        cells = _CELL_RE.findall(row_html)
        if len(cells) < 2:
            continue
        for index in range(0, len(cells) - 1, 2):
            label = _normalize_field_key(_strip_tags(cells[index]))
            if not label:
                continue
            value = _strip_tags(cells[index + 1])
            if not value:
                control_values = _extract_control_values(cells[index + 1])
                value = next((item for item in control_values if item), None)
            if value and label not in field_map:
                field_map[label] = value

    return field_map


def _pick_detail_value(field_map: dict[str, str], *candidates: str) -> Optional[str]:
    for candidate in candidates:
        key = _normalize_field_key(candidate)
        if key and field_map.get(key):
            return field_map[key]
    return None


def parse_backend_record_detail(page_html: str) -> dict[str, Any]:
    field_map = _extract_named_detail_fields(page_html)
    return {
        "birthday": _parse_date_value(
            _pick_detail_value(field_map, "birthday", "birthdate", "生日", "出生日期")
        ),
        "grade": _pick_detail_value(field_map, "grade", "適讀班級", "班別", "就讀班別"),
        "address": _pick_detail_value(field_map, "address", "addr", "地址", "住址", "家庭住址"),
        "district": _pick_detail_value(field_map, "district", "行政區", "地區", "區域"),
        "referrer": _pick_detail_value(field_map, "referrer", "介紹者", "接待人員", "接待老師"),
        "notes": _pick_detail_value(field_map, "notes", "note", "remark", "備註", "其他備註"),
        "parent_response": _pick_detail_value(
            field_map,
            "parentresponse",
            "response",
            "電訪後家長回應",
            "家長回應",
        ),
        "deposit_collector": _pick_detail_value(
            field_map,
            "depositcollector",
            "收預繳人員",
            "收款人員",
        ),
        "has_deposit": _parse_bool(
            _pick_detail_value(field_map, "hasdeposit", "deposit", "是否預繳", "預繳")
        ),
        "enrolled": _parse_bool(
            _pick_detail_value(field_map, "enrolled", "是否報到", "已報到", "是否註冊", "已註冊")
        ),
        "transfer_term": _parse_bool(
            _pick_detail_value(field_map, "transferterm", "轉其他學期", "轉學期")
        ),
    }


def _login_session(http_session: requests.Session) -> None:
    username, password = _get_credentials()
    if not (username and password):
        raise RuntimeError("未設定 IVYKIDS_USERNAME / IVYKIDS_PASSWORD，無法同步義華校官網。")

    response = http_session.post(
        _get_login_url(),
        data={"account": username, "password": password},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    verify = http_session.get(_get_data_url(), timeout=REQUEST_TIMEOUT_SECONDS)
    verify.raise_for_status()
    if "sortable" not in verify.text:
        raise RuntimeError("義華校官網登入失敗，請確認帳號密碼或登入頁面是否變更。")


def _parse_backend_list_row(row_html: str, page_url: str) -> Optional[IvykidsBackendRecord]:
    cells = _CELL_RE.findall(row_html)
    if len(cells) < 7:
        return None

    link_match = re.search(
        r"""(?is)<a\b[^>]*href=(?:"([^"]*form\.php\?id=\d+[^"]*)"|'([^']*form\.php\?id=\d+[^']*)')""",
        row_html,
    )
    href = (link_match.group(1) or link_match.group(2)) if link_match else None
    if not href:
        return None

    detail_url = urljoin(page_url, href)
    match = _DETAIL_LINK_RE.search(detail_url)
    if not match:
        return None

    visit_date = _strip_tags(cells[1])
    return IvykidsBackendRecord(
        external_id=match.group(1),
        status=_strip_tags(cells[0]),
        visit_date=visit_date,
        child_name=_strip_tags(cells[2]),
        phone=_strip_tags(cells[4]),
        source=_strip_tags(cells[5]),
        created_at=_strip_tags(cells[6]),
        detail_url=detail_url,
        month=_parse_month_from_value(visit_date),
    )


def _discover_next_pages(page_html: str, page_url: str) -> list[str]:
    discovered: list[str] = []
    for double_quoted, single_quoted in _NEXT_PAGE_RE.findall(page_html):
        href = double_quoted or single_quoted
        if not href or "page=" not in href:
            continue
        candidate = urljoin(page_url, href)
        parsed = urlparse(candidate)
        if not parse_qs(parsed.query).get("page"):
            continue
        if candidate not in discovered:
            discovered.append(candidate)
    return discovered


def fetch_backend_records(
    max_pages: int = MAX_SYNC_PAGES,
    http_session: Optional[requests.Session] = None,
    authenticated: bool = False,
) -> tuple[list[IvykidsBackendRecord], int]:
    owns_session = http_session is None
    http_session = http_session or _build_requests_session()

    try:
        if not authenticated:
            _login_session(http_session)

        queue = [_get_data_url()]
        visited: set[str] = set()
        record_map: dict[str, IvykidsBackendRecord] = {}
        page_count = 0

        while queue and page_count < max_pages:
            page_url = queue.pop(0)
            if page_url in visited:
                continue
            visited.add(page_url)

            response = http_session.get(page_url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            page_html = response.text
            page_count += 1

            if "sortable" not in page_html and page_count == 1:
                raise RuntimeError("義華校官網列表頁格式異常，無法解析預約資料。")

            for row_html in _LIST_ROW_RE.findall(page_html):
                record = _parse_backend_list_row(row_html, page_url)
                if record and record.external_id not in record_map:
                    record_map[record.external_id] = record

            for candidate in _discover_next_pages(page_html, page_url):
                if candidate not in visited and candidate not in queue:
                    queue.append(candidate)

        records = sorted(
            record_map.values(),
            key=lambda item: (
                int(item.external_id) if str(item.external_id).isdigit() else -1,
                item.created_at or "",
            ),
            reverse=True,
        )
        return records, page_count
    finally:
        if owns_session and hasattr(http_session, "close"):
            http_session.close()


def fetch_backend_record_detail(
    record: IvykidsBackendRecord,
    http_session: Optional[requests.Session] = None,
    authenticated: bool = False,
) -> IvykidsBackendRecord:
    if not record.detail_url:
        return record

    owns_session = http_session is None
    http_session = http_session or _build_requests_session()

    try:
        if not authenticated:
            _login_session(http_session)
        response = http_session.get(record.detail_url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        detail = parse_backend_record_detail(response.text)
        return replace(record, **detail)
    finally:
        if owns_session and hasattr(http_session, "close"):
            http_session.close()


def enrich_backend_records(
    records: Iterable[IvykidsBackendRecord],
    http_session: requests.Session,
) -> list[IvykidsBackendRecord]:
    enriched: list[IvykidsBackendRecord] = []
    for record in records:
        if not record.detail_url:
            enriched.append(record)
            continue
        try:
            enriched.append(fetch_backend_record_detail(record, http_session=http_session, authenticated=True))
        except Exception as exc:  # pragma: no cover - 失敗時保留列表資料即可
            logger.warning("義華校官網明細解析失敗：external_id=%s error=%s", record.external_id, exc)
            enriched.append(record)
    return enriched


def _deserialize_counts(raw_value: Optional[str]) -> dict[str, int]:
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): int(value) for key, value in parsed.items() if isinstance(value, (int, float))}


def _serialize_counts(counts: dict[str, int]) -> str:
    return json.dumps(counts, ensure_ascii=False)


def _get_or_create_sync_state(session) -> RecruitmentSyncState:
    state = session.query(RecruitmentSyncState).filter(
        RecruitmentSyncState.provider_name == IVYKIDS_BACKEND_SOURCE
    ).first()
    if state:
        return state

    state = RecruitmentSyncState(
        provider_name=IVYKIDS_BACKEND_SOURCE,
        provider_label=IVYKIDS_PROVIDER_LABEL,
        sync_in_progress=False,
    )
    session.add(state)
    session.flush()
    return state


def _build_status_payload(state: Optional[RecruitmentSyncState]) -> dict[str, Any]:
    counts = _deserialize_counts(state.last_sync_counts if state else None)
    provider_available = sync_configured()
    scheduler_on = scheduler_configured()
    payload = {
        "provider_available": provider_available,
        "provider_name": IVYKIDS_BACKEND_SOURCE,
        "provider_label": IVYKIDS_PROVIDER_LABEL,
        "scheduler_enabled": scheduler_on,
        "sync_interval_minutes": get_sync_interval_minutes(),
        "sync_in_progress": bool(state.sync_in_progress) if state else False,
        "last_synced_at": state.last_synced_at.isoformat() if state and state.last_synced_at else None,
        "last_sync_status": state.last_sync_status if state else None,
        "last_sync_message": state.last_sync_message if state else None,
        "last_sync_counts": counts,
        "message": state.last_sync_message if state else None,
    }
    if not provider_available:
        payload["message"] = "尚未設定 IVYKIDS_USERNAME / IVYKIDS_PASSWORD，義華校官網同步未啟用。"
    elif provider_available and not scheduler_on and scheduler_requested():
        payload["message"] = "已設定義華校同步帳密，但自動同步尚未啟用。"
    elif provider_available and not scheduler_requested():
        payload["message"] = payload["message"] or "義華校官網手動同步可用，自動同步尚未啟用。"
    return payload


def get_backend_sync_status(session=None) -> dict[str, Any]:
    if session is not None:
        state = _get_or_create_sync_state(session)
        return _build_status_payload(state)

    with session_scope() as owned_session:
        state = _get_or_create_sync_state(owned_session)
        return _build_status_payload(state)


def _is_cancelled_status(status: Optional[str]) -> bool:
    text = _normalize_text(status)
    return bool(text and "取消" in text)


def _merge_text(existing: Optional[str], incoming: Optional[str]) -> Optional[str]:
    return _normalize_text(incoming) or _normalize_text(existing)


def _merge_bool(existing: Optional[bool], incoming: Optional[bool], default: bool = False) -> bool:
    if incoming is not None:
        return bool(incoming)
    if existing is not None:
        return bool(existing)
    return default


def _find_manual_match(session, record: IvykidsBackendRecord) -> Optional[RecruitmentVisit]:
    child_name = _normalize_text(record.child_name)
    phone = _normalize_text(record.phone)
    visit_signature = _normalize_signature_date(record.visit_date)
    if not (child_name and phone and visit_signature):
        return None

    candidates = session.query(RecruitmentVisit).filter(
        RecruitmentVisit.child_name == child_name,
        RecruitmentVisit.phone == phone,
        RecruitmentVisit.external_id.is_(None),
    ).all()
    for candidate in candidates:
        if _normalize_signature_date(candidate.visit_date) == visit_signature:
            return candidate
    return None


def _apply_record_to_visit(visit: RecruitmentVisit, record: IvykidsBackendRecord) -> None:
    visit.month = (
        record.month
        or _parse_month_from_value(record.visit_date)
        or _parse_month_from_value(record.created_at)
        or visit.month
        or f"{datetime.now().year - 1911}.{datetime.now().month:02d}"
    )
    visit.visit_date = _merge_text(visit.visit_date, record.visit_date)
    visit.child_name = _merge_text(visit.child_name, record.child_name) or visit.child_name
    visit.phone = _merge_text(visit.phone, record.phone)
    visit.source = _merge_text(visit.source, record.source)
    visit.external_source = IVYKIDS_BACKEND_SOURCE
    visit.external_id = record.external_id
    visit.external_status = _merge_text(visit.external_status, record.status)
    visit.birthday = record.birthday or visit.birthday
    visit.grade = _merge_text(visit.grade, record.grade)
    visit.address = _merge_text(visit.address, record.address)
    visit.district = _merge_text(visit.district, record.district)
    visit.referrer = _merge_text(visit.referrer, record.referrer)
    visit.notes = _merge_text(visit.notes, record.notes)
    visit.parent_response = _merge_text(visit.parent_response, record.parent_response)
    visit.deposit_collector = _merge_text(visit.deposit_collector, record.deposit_collector)
    visit.has_deposit = _merge_bool(getattr(visit, "has_deposit", None), record.has_deposit, default=False)
    visit.enrolled = _merge_bool(getattr(visit, "enrolled", None), record.enrolled, default=False)
    visit.transfer_term = _merge_bool(
        getattr(visit, "transfer_term", None),
        record.transfer_term,
        default=False,
    )


def _preview_item(record: IvykidsBackendRecord, action: str) -> dict[str, Any]:
    return {
        "external_id": record.external_id,
        "child_name": record.child_name,
        "visit_date": record.visit_date,
        "status": record.status,
        "action": action,
    }


def _busy_sync_result(session) -> dict[str, Any]:
    status = get_backend_sync_status(session)
    counts = status.get("last_sync_counts") or {}
    return {
        "provider_available": status["provider_available"],
        "provider_name": IVYKIDS_BACKEND_SOURCE,
        "sync_success": False,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "total_fetched": 0,
        "page_count": 0,
        "message": "義華校官網同步進行中，請稍後再試。",
        "preview": [],
        "last_synced_at": status.get("last_synced_at"),
        "sync_in_progress": True,
        "scheduler_enabled": status.get("scheduler_enabled"),
        "sync_interval_minutes": status.get("sync_interval_minutes"),
        "last_sync_counts": counts,
    }


def _run_sync(session, max_pages: int, trigger: str) -> dict[str, Any]:
    state = _get_or_create_sync_state(session)
    if not sync_configured():
        status = _build_status_payload(state)
        return {
            "provider_available": False,
            "provider_name": IVYKIDS_BACKEND_SOURCE,
            "sync_success": False,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "total_fetched": 0,
            "page_count": 0,
            "message": status["message"],
            "preview": [],
            "last_synced_at": status.get("last_synced_at"),
            "sync_in_progress": bool(state.sync_in_progress),
            "scheduler_enabled": status.get("scheduler_enabled"),
            "sync_interval_minutes": status.get("sync_interval_minutes"),
            "last_sync_counts": status.get("last_sync_counts"),
        }

    if not _SYNC_LOCK.acquire(blocking=False):
        return _busy_sync_result(session)

    started_at = datetime.now()
    try:
        state.sync_in_progress = True
        state.last_started_at = started_at
        state.last_sync_status = "running"
        state.last_sync_message = f"{IVYKIDS_PROVIDER_LABEL}同步進行中"
        session.flush()

        preview: list[dict[str, Any]] = []
        with _build_requests_session() as http_session:
            _login_session(http_session)
            records, page_count = fetch_backend_records(
                max_pages=max_pages,
                http_session=http_session,
                authenticated=True,
            )
            records = enrich_backend_records(records, http_session=http_session)

        inserted = 0
        updated = 0
        skipped = 0

        for record in records:
            if _is_cancelled_status(record.status):
                skipped += 1
                if len(preview) < PREVIEW_LIMIT:
                    preview.append(_preview_item(record, "skipped"))
                continue

            existing = session.query(RecruitmentVisit).filter(
                RecruitmentVisit.external_source == IVYKIDS_BACKEND_SOURCE,
                RecruitmentVisit.external_id == record.external_id,
            ).first()

            action = "updated"
            if existing is None:
                existing = _find_manual_match(session, record)
                if existing is None:
                    existing = RecruitmentVisit(
                        month=record.month or f"{datetime.now().year - 1911}.{datetime.now().month:02d}",
                        child_name=_normalize_text(record.child_name) or f"外部資料-{record.external_id}",
                        has_deposit=False,
                        enrolled=False,
                        transfer_term=False,
                    )
                    session.add(existing)
                    inserted += 1
                    action = "inserted"
                else:
                    updated += 1
            else:
                updated += 1

            _apply_record_to_visit(existing, record)
            if len(preview) < PREVIEW_LIMIT:
                preview.append(_preview_item(record, action))

        synced_at = datetime.now()
        counts = {
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "total_fetched": len(records),
            "page_count": page_count,
        }
        state.sync_in_progress = False
        state.last_synced_at = synced_at
        state.last_sync_status = "success"
        state.last_sync_message = (
            f"{IVYKIDS_PROVIDER_LABEL}同步完成：新增 {inserted} 筆、更新 {updated} 筆、略過 {skipped} 筆。"
        )
        state.last_sync_counts = _serialize_counts(counts)
        session.flush()

        logger.info(
            "義華校官網同步完成：trigger=%s fetched=%s inserted=%s updated=%s skipped=%s pages=%s duration_ms=%s",
            trigger,
            len(records),
            inserted,
            updated,
            skipped,
            page_count,
            int((synced_at - started_at).total_seconds() * 1000),
        )
        return {
            "provider_available": True,
            "provider_name": IVYKIDS_BACKEND_SOURCE,
            "sync_success": True,
            "inserted": inserted,
            "updated": updated,
            "skipped": skipped,
            "total_fetched": len(records),
            "page_count": page_count,
            "message": state.last_sync_message,
            "preview": preview,
            "last_synced_at": synced_at.isoformat(),
            "sync_in_progress": False,
            "scheduler_enabled": scheduler_configured(),
            "sync_interval_minutes": get_sync_interval_minutes(),
            "last_sync_counts": counts,
        }
    except Exception as exc:
        logger.exception("義華校官網同步失敗：trigger=%s error=%s", trigger, exc)
        session.rollback()
        state = _get_or_create_sync_state(session)
        state.sync_in_progress = False
        state.last_sync_status = "failed"
        state.last_sync_message = str(exc)
        state.last_sync_counts = _serialize_counts({
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "total_fetched": 0,
            "page_count": 0,
        })
        session.flush()
        return {
            "provider_available": True,
            "provider_name": IVYKIDS_BACKEND_SOURCE,
            "sync_success": False,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "total_fetched": 0,
            "page_count": 0,
            "message": str(exc),
            "preview": [],
            "last_synced_at": state.last_synced_at.isoformat() if state.last_synced_at else None,
            "sync_in_progress": False,
            "scheduler_enabled": scheduler_configured(),
            "sync_interval_minutes": get_sync_interval_minutes(),
            "last_sync_counts": _deserialize_counts(state.last_sync_counts),
        }
    finally:
        _SYNC_LOCK.release()


def sync_backend_records(session=None, max_pages: int = MAX_SYNC_PAGES, trigger: str = "manual") -> dict[str, Any]:
    if session is not None:
        return _run_sync(session, max_pages=max_pages, trigger=trigger)

    with session_scope() as owned_session:
        return _run_sync(owned_session, max_pages=max_pages, trigger=trigger)


async def run_sync_scheduler(stop_event: asyncio.Event) -> None:
    if not scheduler_configured():
        logger.info("義華校官網自動同步未啟用")
        return

    interval_seconds = get_sync_interval_minutes() * 60
    logger.info("義華校官網自動同步已啟用，每 %s 分鐘執行一次", get_sync_interval_minutes())

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            break
        except asyncio.TimeoutError:
            try:
                await asyncio.to_thread(sync_backend_records, None, MAX_SYNC_PAGES, "scheduler")
            except Exception:  # pragma: no cover - 保險用
                logger.exception("義華校官網排程同步發生未預期錯誤")
