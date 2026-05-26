"""
驗 GET /portal/me/comp-leave-grants + GET /portal/me/payout-history

D-2-BE：員工自己補休歷史明細
- grants：全狀態 grant ledger，granted_at DESC
- payout：unused_leave_payout_log，created_at DESC
"""

import sys
import os
import types
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _portal_user(emp_id: int = 10) -> dict:
    return {"username": f"teacher{emp_id}", "employee_id": emp_id}


def _make_emp(emp_id: int = 10):
    emp = types.SimpleNamespace()
    emp.id = emp_id
    emp.name = "測試教師"
    emp.hire_date = date(2020, 1, 1)
    return emp


def _make_grant(
    grant_id: int,
    granted_hours: float = 8.0,
    consumed_hours: float = 0.0,
    granted_at: date = date(2026, 1, 1),
    expires_at: date = date(2027, 1, 1),
    status: str = "active",
    expired_at=None,
):
    g = types.SimpleNamespace()
    g.id = grant_id
    g.granted_hours = granted_hours
    g.consumed_hours = consumed_hours
    g.granted_at = granted_at
    g.expires_at = expires_at
    g.status = status
    g.expired_at = expired_at
    return g


def _make_payout_log(
    log_id: int,
    source_type: str = "comp_grant_expiry",
    hours: float = 4.0,
    hourly_wage=Decimal("200.00"),
    amount=Decimal("800.00"),
    wage_basis_date: date = date(2026, 5, 1),
    salary_period_year: int = 2026,
    salary_period_month: int = 6,
    meta: dict | None = None,
    created_at: datetime | None = None,
):
    log = types.SimpleNamespace()
    log.id = log_id
    log.source_type = source_type
    log.hours = hours
    log.hourly_wage = hourly_wage
    log.amount = amount
    log.wage_basis_date = wage_basis_date
    log.salary_period_year = salary_period_year
    log.salary_period_month = salary_period_month
    log.meta = meta if meta is not None else {}
    log.created_at = created_at or datetime(2026, 5, 15, 12, 0, 0)
    return log


# ---------------------------------------------------------------------------
# Helper: 取 endpoint functions
# ---------------------------------------------------------------------------


def _get_grants_endpoint():
    import api.portal.comp_leave_history as mod

    return mod.list_my_comp_leave_grants


def _get_payout_endpoint():
    import api.portal.comp_leave_history as mod

    return mod.list_my_payout_history


# ---------------------------------------------------------------------------
# Test class: comp-leave-grants
# ---------------------------------------------------------------------------


class TestCompLeaveGrants:
    """GET /portal/me/comp-leave-grants"""

    def test_grants_list_orders_by_granted_at_desc(self):
        """3 grants 不同 granted_at → response 依 desc 排序（最近在前）。"""
        import api.portal.comp_leave_history as mod

        emp = _make_emp(emp_id=10)
        # 建三筆 grant，granted_at 不同
        grant_old = _make_grant(
            1,
            granted_at=date(2025, 3, 1),
            expires_at=date(2026, 3, 1),
            status="expired",
        )
        grant_mid = _make_grant(
            2, granted_at=date(2025, 8, 15), expires_at=date(2026, 8, 15)
        )
        grant_new = _make_grant(
            3, granted_at=date(2026, 1, 10), expires_at=date(2027, 1, 10)
        )
        # 模擬 DB 已按 granted_at DESC 回傳（endpoint 透過 .order_by 保證）
        ordered_grants = [grant_new, grant_mid, grant_old]

        session = MagicMock()

        with (
            patch.object(mod, "_get_employee", return_value=emp),
            patch.object(mod, "_query_grants", return_value=ordered_grants),
        ):
            result = mod.list_my_comp_leave_grants(
                session=session, current_user=_portal_user(10)
            )

        grants = result["grants"]
        assert len(grants) == 3
        # 最新在前
        assert grants[0]["grant_id"] == 3
        assert grants[1]["grant_id"] == 2
        assert grants[2]["grant_id"] == 1

    def test_grants_list_includes_all_status(self):
        """active / expired / revoked 全部回傳，不過濾。"""
        import api.portal.comp_leave_history as mod

        emp = _make_emp(emp_id=10)
        grant_active = _make_grant(1, status="active")
        grant_expired = _make_grant(
            2,
            status="expired",
            expired_at=datetime(2026, 3, 1, 8, 0, 0),
        )
        grant_revoked = _make_grant(3, status="revoked")
        all_grants = [grant_active, grant_expired, grant_revoked]

        session = MagicMock()

        with (
            patch.object(mod, "_get_employee", return_value=emp),
            patch.object(mod, "_query_grants", return_value=all_grants),
        ):
            result = mod.list_my_comp_leave_grants(
                session=session, current_user=_portal_user(10)
            )

        statuses = {g["status"] for g in result["grants"]}
        assert statuses == {"active", "expired", "revoked"}

    def test_grants_list_expired_at_is_iso_string_or_null(self):
        """expired_at：非 None → ISO datetime 字串；None → null。"""
        import api.portal.comp_leave_history as mod

        emp = _make_emp(emp_id=10)
        g_with_expired = _make_grant(
            1, status="expired", expired_at=datetime(2026, 4, 1, 10, 30, 0)
        )
        g_no_expired = _make_grant(2, status="active", expired_at=None)

        session = MagicMock()

        with (
            patch.object(mod, "_get_employee", return_value=emp),
            patch.object(
                mod, "_query_grants", return_value=[g_with_expired, g_no_expired]
            ),
        ):
            result = mod.list_my_comp_leave_grants(
                session=session, current_user=_portal_user(10)
            )

        grants = result["grants"]
        assert grants[0]["expired_at"] == "2026-04-01T10:30:00"
        assert grants[1]["expired_at"] is None

    def test_grants_remaining_hours_computed_correctly(self):
        """remaining_hours = granted_hours - consumed_hours。"""
        import api.portal.comp_leave_history as mod

        emp = _make_emp(emp_id=10)
        g = _make_grant(1, granted_hours=10.0, consumed_hours=3.5)

        session = MagicMock()

        with (
            patch.object(mod, "_get_employee", return_value=emp),
            patch.object(mod, "_query_grants", return_value=[g]),
        ):
            result = mod.list_my_comp_leave_grants(
                session=session, current_user=_portal_user(10)
            )

        assert result["grants"][0]["remaining_hours"] == pytest.approx(6.5)

    def test_grants_excludes_other_employees(self):
        """emp_id=10 只看到自己的 grants；emp_id=99 的 grants 不出現。"""
        import api.portal.comp_leave_history as mod

        emp_a = _make_emp(emp_id=10)
        # _query_grants 會以 emp_id filter，模擬只回 emp_a 的 grant
        grant_a = _make_grant(1)

        session = MagicMock()

        with (
            patch.object(mod, "_get_employee", return_value=emp_a),
            patch.object(mod, "_query_grants", return_value=[grant_a]) as mock_query,
        ):
            result = mod.list_my_comp_leave_grants(
                session=session, current_user=_portal_user(10)
            )
            # 確認 _query_grants 以 emp_a.id=10 呼叫
            mock_query.assert_called_once_with(session, 10)

        assert len(result["grants"]) == 1
        assert result["grants"][0]["grant_id"] == 1

    def test_empty_grants_returns_empty_list(self):
        """無 grants → {"grants": []}。"""
        import api.portal.comp_leave_history as mod

        emp = _make_emp(emp_id=10)
        session = MagicMock()

        with (
            patch.object(mod, "_get_employee", return_value=emp),
            patch.object(mod, "_query_grants", return_value=[]),
        ):
            result = mod.list_my_comp_leave_grants(
                session=session, current_user=_portal_user(10)
            )

        assert result == {"grants": []}


# ---------------------------------------------------------------------------
# Test class: payout-history
# ---------------------------------------------------------------------------


class TestPayoutHistory:
    """GET /portal/me/payout-history"""

    def test_payout_history_orders_by_created_at_desc(self):
        """多筆 payout log → created_at DESC 排序（最新在前）。"""
        import api.portal.comp_leave_history as mod

        emp = _make_emp(emp_id=10)
        log_old = _make_payout_log(1, created_at=datetime(2026, 3, 1))
        log_new = _make_payout_log(2, created_at=datetime(2026, 5, 20))
        # 模擬 DB 已按 created_at DESC 回傳
        ordered_logs = [log_new, log_old]

        session = MagicMock()

        with (
            patch.object(mod, "_get_employee", return_value=emp),
            patch.object(mod, "_query_payout_logs", return_value=ordered_logs),
        ):
            result = mod.list_my_payout_history(
                session=session, current_user=_portal_user(10)
            )

        logs = result["logs"]
        assert len(logs) == 2
        assert logs[0]["log_id"] == 2  # 最新在前
        assert logs[1]["log_id"] == 1

    def test_payout_history_excludes_other_employees(self):
        """emp_id=10 的查詢，_query_payout_logs 以 10 呼叫；B 員工不混入。"""
        import api.portal.comp_leave_history as mod

        emp_a = _make_emp(emp_id=10)
        log_a = _make_payout_log(1)

        session = MagicMock()

        with (
            patch.object(mod, "_get_employee", return_value=emp_a),
            patch.object(mod, "_query_payout_logs", return_value=[log_a]) as mock_query,
        ):
            result = mod.list_my_payout_history(
                session=session, current_user=_portal_user(10)
            )
            mock_query.assert_called_once_with(session, 10)

        assert len(result["logs"]) == 1

    def test_payout_history_fields_serialization(self):
        """salary_period 格式 = YYYY-MM；hourly_wage/amount 為 float；meta 為 dict。"""
        import api.portal.comp_leave_history as mod

        emp = _make_emp(emp_id=10)
        log = _make_payout_log(
            1,
            source_type="annual_anniversary",
            hours=8.0,
            hourly_wage=Decimal("250.50"),
            amount=Decimal("2004.00"),
            wage_basis_date=date(2026, 4, 30),
            salary_period_year=2026,
            salary_period_month=5,
            meta={"reason": "週年結算"},
        )

        session = MagicMock()

        with (
            patch.object(mod, "_get_employee", return_value=emp),
            patch.object(mod, "_query_payout_logs", return_value=[log]),
        ):
            result = mod.list_my_payout_history(
                session=session, current_user=_portal_user(10)
            )

        entry = result["logs"][0]
        assert entry["log_id"] == 1
        assert entry["source_type"] == "annual_anniversary"
        assert entry["hours"] == pytest.approx(8.0)
        assert entry["hourly_wage"] == pytest.approx(250.50)
        assert entry["amount"] == pytest.approx(2004.00)
        assert entry["wage_basis_date"] == "2026-04-30"
        assert entry["salary_period"] == "2026-05"
        assert entry["meta"] == {"reason": "週年結算"}

    def test_payout_history_salary_period_zero_padding(self):
        """salary_period_month=5 → '2026-05'（零補位）。"""
        import api.portal.comp_leave_history as mod

        emp = _make_emp(emp_id=10)
        log = _make_payout_log(1, salary_period_year=2026, salary_period_month=5)

        session = MagicMock()

        with (
            patch.object(mod, "_get_employee", return_value=emp),
            patch.object(mod, "_query_payout_logs", return_value=[log]),
        ):
            result = mod.list_my_payout_history(
                session=session, current_user=_portal_user(10)
            )

        assert result["logs"][0]["salary_period"] == "2026-05"

    def test_empty_payout_history_returns_empty_list(self):
        """無 logs → {"logs": []}。"""
        import api.portal.comp_leave_history as mod

        emp = _make_emp(emp_id=10)
        session = MagicMock()

        with (
            patch.object(mod, "_get_employee", return_value=emp),
            patch.object(mod, "_query_payout_logs", return_value=[]),
        ):
            result = mod.list_my_payout_history(
                session=session, current_user=_portal_user(10)
            )

        assert result == {"logs": []}
