"""政府開放資料 HTTP 抓取 + snapshot 落地。

公開介面：
- fetch_one(source, url) -> snapshot_id
- fetch_all() -> dict[source, snapshot_id]

所有結果（成功 / 失敗）都落地 gov_data_snapshots，便於稽核與排查。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

import requests

from models.database import GovDataSnapshot, session_scope
from services.gov_data.utils import sha256_of_payload

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 30
DEFAULT_MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 2

# source -> URL 對照
# 注意：T0 fixture 使用的真實 URL 在 tests/fixtures/gov_data/_README.md 內有記錄。
# 此 dict 的初始值由 ops 在部署前以實際可用的 URL 填入；測試環境用 fetch_one 直接傳 url 不依賴此 dict。
SOURCE_URLS: dict[str, str] = {
    "mol_labor_brackets": "",
    "mol_labor_premium": "",
    "mol_pension": "",
    "nhi_brackets": "",
    "nhi_premium": "",
    "mol_minimum_wage": "",
}


def fetch_one(source: str, url: str, max_retries: int = DEFAULT_MAX_RETRIES) -> int:
    """抓單一 source，落地 snapshot；回傳 snapshot id。

    成功：寫入 raw_payload + payload_hash；若 hash 與該 source 最新成功 snapshot 同，
    不重複寫入，回傳舊 id。
    失敗：寫入 error 欄位、http_status；回傳新 id（仍需稽核）。
    Retry：ConnectionError / Timeout 重試最多 `max_retries` 次。
    """
    last_exc: Optional[BaseException] = None
    response: Optional[requests.Response] = None
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=DEFAULT_TIMEOUT_SEC)
            break
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            logger.warning("fetch %s attempt %d failed: %s", source, attempt + 1, exc)
            if attempt < max_retries - 1:
                time.sleep(RETRY_BACKOFF_SEC * (2**attempt))
    if response is None:
        return _write_error_snapshot(source, url, http_status=0, error=str(last_exc))

    if response.status_code != 200:
        return _write_error_snapshot(
            source,
            url,
            http_status=response.status_code,
            error=response.text[:1000],
        )

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        return _write_error_snapshot(
            source, url, http_status=response.status_code, error=f"invalid json: {exc}"
        )

    payload_hash = sha256_of_payload(payload)
    with session_scope() as s:
        latest = (
            s.query(GovDataSnapshot)
            .filter(
                GovDataSnapshot.source == source, GovDataSnapshot.http_status == 200
            )
            .order_by(GovDataSnapshot.fetched_at.desc())
            .first()
        )
        if latest and latest.payload_hash == payload_hash:
            logger.info("skip %s: hash unchanged (%s)", source, payload_hash[:8])
            return latest.id
        snap = GovDataSnapshot(
            source=source,
            source_url=url,
            http_status=200,
            raw_payload=payload,
            payload_hash=payload_hash,
        )
        s.add(snap)
        s.flush()
        return snap.id


def _write_error_snapshot(source: str, url: str, http_status: int, error: str) -> int:
    with session_scope() as s:
        snap = GovDataSnapshot(
            source=source,
            source_url=url,
            http_status=http_status,
            raw_payload=None,
            payload_hash=sha256_of_payload({"error": error[:500]}),
            error=error[:5000],
        )
        s.add(snap)
        s.flush()
        return snap.id


def fetch_all() -> dict[str, int]:
    """抓全部 6 個 source；單一 source 失敗不阻塞其他。

    使用 SOURCE_URLS 的 URL；若任一 source 的 url 為空字串，該 source 跳過。
    """
    result: dict[str, int] = {}
    for source, url in SOURCE_URLS.items():
        if not url:
            logger.warning("skip %s: SOURCE_URLS empty (ops 未配置實際 URL)", source)
            continue
        try:
            result[source] = fetch_one(source, url)
        except Exception:
            logger.exception("unexpected error fetching %s", source)
            result[source] = _write_error_snapshot(
                source, url, http_status=0, error="unexpected exception"
            )
    return result
