"""fetcher：HTTP 抓取 + snapshot 落地測試。"""

import json
from pathlib import Path

import pytest
import responses

from services.gov_data import fetcher
from models.database import GovDataSnapshot, session_scope

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "gov_data"


@pytest.fixture
def labor_brackets_raw():
    return json.loads(
        (FIXTURE_DIR / "mol_labor_brackets_2026.json").read_text(encoding="utf-8")
    )


@responses.activate
def test_fetch_one_success_writes_snapshot(test_db_session, labor_brackets_raw):
    url = "https://example.com/mol_labor_brackets"
    responses.add(responses.GET, url, json=labor_brackets_raw, status=200)
    snap_id = fetcher.fetch_one("mol_labor_brackets", url)
    with session_scope() as s:
        snap = s.get(GovDataSnapshot, snap_id)
        assert snap.source == "mol_labor_brackets"
        assert snap.http_status == 200
        assert snap.raw_payload == labor_brackets_raw
        assert len(snap.payload_hash) == 64
        assert snap.error is None


@responses.activate
def test_fetch_one_500_writes_error_snapshot(test_db_session):
    url = "https://example.com/down"
    responses.add(responses.GET, url, json={"err": "down"}, status=500)
    snap_id = fetcher.fetch_one("mol_labor_brackets", url)
    with session_scope() as s:
        snap = s.get(GovDataSnapshot, snap_id)
        assert snap.http_status == 500
        assert snap.error is not None
        assert snap.raw_payload is None


@responses.activate
def test_fetch_one_skip_when_hash_unchanged(test_db_session, labor_brackets_raw):
    url = "https://example.com/mol_labor_brackets"
    responses.add(responses.GET, url, json=labor_brackets_raw, status=200)
    responses.add(responses.GET, url, json=labor_brackets_raw, status=200)

    first = fetcher.fetch_one("mol_labor_brackets", url)
    second = fetcher.fetch_one("mol_labor_brackets", url)
    # 同 hash：第二次回傳 first 的 id，不寫新列
    assert second == first
    with session_scope() as s:
        count = s.query(GovDataSnapshot).filter_by(source="mol_labor_brackets").count()
        assert count == 1


@responses.activate
def test_fetch_one_retries_on_connection_error(test_db_session, labor_brackets_raw):
    """前 1 次連線錯誤，第 2 次成功；snapshot 應記錄第 2 次的 200。"""
    import requests

    url = "https://example.com/flaky"
    responses.add(responses.GET, url, body=requests.ConnectionError("boom"))
    responses.add(responses.GET, url, json=labor_brackets_raw, status=200)
    snap_id = fetcher.fetch_one("mol_labor_brackets", url, max_retries=2)
    with session_scope() as s:
        snap = s.get(GovDataSnapshot, snap_id)
        assert snap.http_status == 200
