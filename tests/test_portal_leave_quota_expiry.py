"""
Tests for GET /portal/me/leave-quota-expiry

C3-1: 員工自助入口 - 補休結餘 + 最早到期 + 下個結算月預告
"""

import sys
import os
import types
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_emp(hire_date=date(2020, 6, 15), emp_id=42):
    """建立假 Employee SimpleNamespace。"""
    emp = types.SimpleNamespace()
    emp.id = emp_id
    emp.name = "測試教師"
    emp.hire_date = hire_date
    return emp


def _make_grant(
    expires_at: date, granted_hours: float = 8.0, consumed_hours: float = 2.0
):
    """建立假 OvertimeCompLeaveGrant SimpleNamespace。"""
    g = types.SimpleNamespace()
    g.expires_at = expires_at
    g.granted_hours = granted_hours
    g.consumed_hours = consumed_hours
    return g


def _portal_user(emp_id=42):
    return {"username": "teacher42", "employee_id": emp_id}


# ---------------------------------------------------------------------------
# Helper: 取 endpoint function
# ---------------------------------------------------------------------------


def _get_endpoint():
    """延遲 import，確保 patch 可以疊在前面。"""
    from api.portal import leaves as _  # ensure module loaded
    from api.portal import leaves_quota_expiry as mod

    return mod.get_my_leave_quota_expiry


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestLeaveQuotaExpiry:
    """GET /portal/me/leave-quota-expiry 正常 + 邊界場景。"""

    # --- Case 1: 有 grant + 有週年 -------------------------------------------

    def test_full_response_with_grants_and_anniversary(self):
        """有 active grant + 有 hire_date → 完整 response。

        Scenario:
          - today = 2026-05-26
          - hire_date = 2020-06-15 → next_anniversary = 2026-06-15
          - earliest grant expires_at = 2026-07-01, granted=8h consumed=2h
          - balance = 6.0
          - candidates: [2026-07-01, 2026-06-15] → closest = 2026-06-15
          - expected_payout_month = _next_month(2026-06-15) = (2026, 7) → "2026-07"
        """
        import api.portal.leaves_quota_expiry as mod

        emp = _make_emp(hire_date=date(2020, 6, 15), emp_id=42)
        grant = _make_grant(
            expires_at=date(2026, 7, 1), granted_hours=8.0, consumed_hours=2.0
        )

        session = MagicMock()
        # _compensatory_balance patch → 6.0
        # OvertimeCompLeaveGrant query → [grant]

        with (
            patch.object(mod, "_get_employee", return_value=emp),
            patch.object(mod, "_compensatory_balance", return_value=6.0),
            patch.object(mod, "_get_earliest_active_grant", return_value=grant),
            patch(
                "api.portal.leaves_quota_expiry.today_taipei",
                return_value=date(2026, 5, 26),
            ),
        ):
            result = mod.get_my_leave_quota_expiry(
                session=session,
                current_user=_portal_user(42),
            )

        assert result["compensatory_balance"] == 6.0
        assert result["earliest_expiring_grant"] is not None
        assert result["earliest_expiring_grant"]["expires_at"] == "2026-07-01"
        assert result["earliest_expiring_grant"]["unexpired_hours"] == pytest.approx(
            6.0
        )
        # next_anniversary: hire_date=2020-06-15, today=2026-05-26
        # (05,26) < (06,15) → 還沒過本年週年 → years=6, anniv=2026-06-15
        assert result["next_anniversary"] == "2026-06-15"
        # closest of [2026-07-01, 2026-06-15] = 2026-06-15 → next_month = 2026-07
        assert result["expected_payout_month"] == "2026-07"

    # --- Case 2: 無 grant，balance=0 -----------------------------------------

    def test_no_grants_zero_balance(self):
        """無 active grant → compensatory_balance=0, earliest_expiring_grant=null。"""
        import api.portal.leaves_quota_expiry as mod

        emp = _make_emp(hire_date=date(2018, 3, 10), emp_id=7)

        session = MagicMock()

        with (
            patch.object(mod, "_get_employee", return_value=emp),
            patch.object(mod, "_compensatory_balance", return_value=0.0),
            patch.object(mod, "_get_earliest_active_grant", return_value=None),
            patch(
                "api.portal.leaves_quota_expiry.today_taipei",
                return_value=date(2026, 5, 26),
            ),
        ):
            result = mod.get_my_leave_quota_expiry(
                session=session,
                current_user=_portal_user(7),
            )

        assert result["compensatory_balance"] == 0.0
        assert result["earliest_expiring_grant"] is None
        # hire_date=2018-03-10, today=2026-05-26 → (05,26) > (03,10) → years=8+1=9 → 2027-03-10
        assert result["next_anniversary"] == "2027-03-10"
        # only anniversary candidate → expected_payout_month = next_month(2027-03-10) = 2027-04
        assert result["expected_payout_month"] == "2027-04"

    # --- Case 3: 2/29 hire_date -------------------------------------------

    def test_feb29_hire_date_fallback(self):
        """hire_date=2000-02-29（閏年）→ 2026 非閏年 → next_anniversary = 2026-02-28。"""
        import api.portal.leaves_quota_expiry as mod

        emp = _make_emp(hire_date=date(2000, 2, 29), emp_id=99)
        grant = _make_grant(expires_at=date(2026, 12, 31))

        session = MagicMock()

        with (
            patch.object(mod, "_get_employee", return_value=emp),
            patch.object(mod, "_compensatory_balance", return_value=4.0),
            patch.object(mod, "_get_earliest_active_grant", return_value=grant),
            patch(
                "api.portal.leaves_quota_expiry.today_taipei",
                return_value=date(2026, 5, 26),
            ),
        ):
            # today = 2026-05-26; (05,26) > (02,28) → years=26+1=27? No:
            # hire_date 02/29, today 05/26 → 05>02 → years=today.year-hire.year+1? Let's be precise.
            # today=2026-05-26, hire=2000-02-29
            # years = 2026 - 2000 = 26; (5,26) >= (2,29)? → 5>2 so yes → years=27? No wait:
            # (today.month, today.day) >= (hire_date.month, hire_date.day)?
            # (5,26) >= (2,29)? 5 > 2 → True → years += 1 → years=27
            # next_anniv year = 2000 + 27 = 2027 → 2027-02-29 → not leap → 2027-02-28

            result = mod.get_my_leave_quota_expiry(
                session=session,
                current_user=_portal_user(99),
            )

        assert result["next_anniversary"] == "2027-02-28"
        # candidates: [2026-12-31, 2027-02-28] → closest = 2026-12-31
        # next_month(2026-12-31) → (2027, 1)
        assert result["expected_payout_month"] == "2027-01"

    # --- Case 4: hire_date is None -------------------------------------------

    def test_no_hire_date_null_anniversary(self):
        """emp.hire_date is None → next_anniversary=null, expected_payout_month=null（無 grant 時）。"""
        import api.portal.leaves_quota_expiry as mod

        emp = _make_emp(hire_date=None, emp_id=55)
        emp.hire_date = None  # explicit

        session = MagicMock()

        with (
            patch.object(mod, "_get_employee", return_value=emp),
            patch.object(mod, "_compensatory_balance", return_value=0.0),
            patch.object(mod, "_get_earliest_active_grant", return_value=None),
            patch(
                "api.portal.leaves_quota_expiry.today_taipei",
                return_value=date(2026, 5, 26),
            ),
        ):
            result = mod.get_my_leave_quota_expiry(
                session=session,
                current_user=_portal_user(55),
            )

        assert result["next_anniversary"] is None
        assert result["expected_payout_month"] is None
        assert result["compensatory_balance"] == 0.0
        assert result["earliest_expiring_grant"] is None
