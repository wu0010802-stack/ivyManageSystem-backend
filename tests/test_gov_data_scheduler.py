"""scheduler：sync_now 觸發 fetch_all + composer + staging 落地。"""

from unittest.mock import patch

import pytest

from models.database import (
    GovDataSnapshot,
    InsuranceBracketsStaging,
    MinimumWageStaging,
    session_scope,
)
from services import gov_data_scheduler


@patch("services.gov_data_scheduler.fetcher.fetch_all")
@patch("services.gov_data_scheduler._compose_and_stage_brackets")
@patch("services.gov_data_scheduler._compose_and_stage_minimum_wage")
def test_sync_now_calls_fetch_then_compose(
    mock_mw, mock_brackets, mock_fetch, test_db_session
):
    mock_fetch.return_value = {
        "mol_labor_brackets": 1,
        "mol_labor_premium": 2,
        "mol_pension": 3,
        "nhi_brackets": 4,
        "nhi_premium": 5,
        "mol_minimum_wage": 6,
    }
    mock_brackets.return_value = None
    mock_mw.return_value = None
    result = gov_data_scheduler.sync_now()
    mock_fetch.assert_called_once()
    mock_brackets.assert_called_once()
    mock_mw.assert_called_once()
    assert "snapshot_ids" in result


@patch("services.gov_data_scheduler.fetcher.fetch_all")
def test_sync_now_skips_compose_if_brackets_source_failed(mock_fetch, test_db_session):
    """5 個合成源任一寫的是 error snapshot 時，brackets 與 minimum_wage 應跳過 staging。"""
    with session_scope() as s:
        err_snap = GovDataSnapshot(
            source="mol_labor_brackets",
            source_url="x",
            http_status=500,
            payload_hash="x" * 64,
            error="500",
            raw_payload=None,
        )
        s.add(err_snap)
        s.flush()
        err_id = err_snap.id

    # mol_minimum_wage 也用同一個 error snapshot 測 minimum_wage 也跳過
    mock_fetch.return_value = {
        "mol_labor_brackets": err_id,
        "mol_minimum_wage": err_id,
    }

    gov_data_scheduler.sync_now()

    with session_scope() as s:
        assert s.query(InsuranceBracketsStaging).count() == 0
        assert s.query(MinimumWageStaging).count() == 0


def test_is_enabled_default_false(monkeypatch):
    monkeypatch.delenv("GOV_DATA_SYNC_ENABLED", raising=False)
    assert gov_data_scheduler.is_enabled() is False


def test_is_enabled_when_set(monkeypatch):
    monkeypatch.setenv("GOV_DATA_SYNC_ENABLED", "1")
    assert gov_data_scheduler.is_enabled() is True
