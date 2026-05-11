"""promoter：staging → 正式表 + mark_stale + reload 測試。"""

from datetime import date

import pytest

from models.database import (
    InsuranceBracket,
    InsuranceBracketsStaging,
    MinimumWageHistory,
    MinimumWageStaging,
    SalaryRecord,
    session_scope,
)
from services.gov_data import promoter


def test_promote_brackets_writes_production(pending_brackets_staging):
    promoter.promote_brackets(
        staging_id=pending_brackets_staging,
        decided_by="admin",
        reason="2027 年度政府公告，已比對 diff，無異常 — 套用",
    )
    with session_scope() as s:
        rows = (
            s.query(InsuranceBracket)
            .filter(InsuranceBracket.effective_year == 2027)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].amount == 30000
        st = s.get(InsuranceBracketsStaging, pending_brackets_staging)
        assert st.status == "promoted"
        assert st.decided_by == "admin"
        assert st.decision_reason.startswith("2027 年度政府公告")


def test_promote_brackets_marks_salary_stale(
    pending_brackets_staging, sample_unfinalized_salary_2027
):
    """promote 後，2027 年所有未封存 SalaryRecord 應 needs_recalc=True。"""
    rec_id = sample_unfinalized_salary_2027.id
    promoter.promote_brackets(
        staging_id=pending_brackets_staging,
        decided_by="admin",
        reason="2027 年度政府公告，已比對 diff，無異常 — 套用",
    )
    with session_scope() as s:
        rec = s.get(SalaryRecord, rec_id)
        assert rec.needs_recalc is True


def test_promote_idempotent_returns_409(pending_brackets_staging):
    promoter.promote_brackets(
        staging_id=pending_brackets_staging,
        decided_by="admin",
        reason="第一次 promote 套用 2027 年級距",
    )
    with pytest.raises(promoter.PromoteError) as exc:
        promoter.promote_brackets(
            staging_id=pending_brackets_staging,
            decided_by="admin",
            reason="第二次重複 promote 應失敗",
        )
    assert exc.value.status_code == 409


def test_promote_reason_too_short_rejected(pending_brackets_staging):
    with pytest.raises(promoter.PromoteError) as exc:
        promoter.promote_brackets(
            staging_id=pending_brackets_staging,
            decided_by="admin",
            reason="太短",
        )
    assert exc.value.status_code == 400


def test_dismiss_marks_staging_dismissed(pending_brackets_staging):
    promoter.dismiss_brackets(
        staging_id=pending_brackets_staging,
        decided_by="admin",
        reason="政府資料異常，先忽略此版等下次更新",
    )
    with session_scope() as s:
        st = s.get(InsuranceBracketsStaging, pending_brackets_staging)
        assert st.status == "dismissed"
        rows = (
            s.query(InsuranceBracket)
            .filter(InsuranceBracket.effective_year == 2027)
            .all()
        )
        assert rows == []


def test_promote_minimum_wage_writes_history(sample_minimum_wage_staging_2027):
    promoter.promote_minimum_wage(
        staging_id=sample_minimum_wage_staging_2027,
        decided_by="admin",
        reason="2027/1/1 基本工資新公告，比對無異常套用",
    )
    with session_scope() as s:
        row = (
            s.query(MinimumWageHistory)
            .filter(MinimumWageHistory.effective_date == date(2027, 1, 1))
            .first()
        )
        assert row is not None
        assert row.confirmed_by == "admin"
        st = s.get(MinimumWageStaging, sample_minimum_wage_staging_2027)
        assert st.status == "promoted"
