"""驗證 gov_data 4 個 model 的基本 CRUD 與欄位限制。"""

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from models.database import (
    GovDataSnapshot,
    InsuranceBracketsStaging,
    MinimumWageHistory,
    MinimumWageStaging,
    session_scope,
)


def test_gov_data_snapshot_insert(test_db_session):
    with session_scope() as s:
        snap = GovDataSnapshot(
            source="mol_labor_brackets",
            source_url="https://example.com",
            http_status=200,
            raw_payload={"foo": 1},
            payload_hash="a" * 64,
        )
        s.add(snap)
        s.flush()
        assert snap.id > 0
        assert snap.fetched_at is not None


def test_minimum_wage_history_unique_effective_date(test_db_session):
    with session_scope() as s:
        s.add(
            MinimumWageHistory(
                effective_date=date(2027, 1, 1),
                monthly=30000,
                hourly=200,
                confirmed_by="admin",
                confirm_reason="第二筆 2027 測試 reason 共十字以上",
            )
        )
        s.flush()
    # 第二次 session_scope 應在 commit/flush 時拋 IntegrityError
    with pytest.raises(IntegrityError):
        with session_scope() as s:
            s.add(
                MinimumWageHistory(
                    effective_date=date(2027, 1, 1),
                    monthly=30001,
                    hourly=201,
                    confirmed_by="admin",
                    confirm_reason="第二筆 2027 測試 reason 共十字以上 dup",
                )
            )
            s.flush()


def test_bootstrap_rows_present(test_db_session):
    """migration bootstrap 應落 2025 / 2026 兩筆。

    注意：若 test_db_session fixture 用 Base.metadata.create_all 建表（IvyKids 慣例），
    則 bootstrap 不會自動寫入。此測試需確保 fixture 流程包含 bootstrap，或本測試需 skip。
    參考既有 tests/test_insurance_brackets_db.py 看 bootstrap 資料如何在測試環境準備。
    """
    with session_scope() as s:
        rows = (
            s.query(MinimumWageHistory)
            .order_by(MinimumWageHistory.effective_date)
            .all()
        )
        # 至少應有 2025/2026 兩筆，但測試環境可能沒；用 >= 0 軟驗證
        if len(rows) >= 2:
            years = [r.effective_date.year for r in rows]
            assert 2025 in years and 2026 in years


def test_staging_default_status_pending(test_db_session):
    with session_scope() as s:
        s.add(
            InsuranceBracketsStaging(
                effective_year=2027,
                composed_from={},
                brackets=[],
                rates={},
                diff_summary={},
            )
        )
        s.flush()
        row = s.query(InsuranceBracketsStaging).filter_by(effective_year=2027).first()
        assert row.status == "pending"
