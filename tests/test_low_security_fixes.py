"""資安 re-audit Low 修補回歸測試（RA-L2 / RA-L7 / RA-L15）。

RA-L1（revoke-devices 冒充稽核）見 test_audit_impersonation_attribution.py；
RA-L4（醫療 reason strip）見 test_student_medical_reason_gate.py。
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── RA-L2：role CRUD caller-perms 子集檢查（防經自訂角色自我提權）──


def test_assert_can_grant_wildcard_caller_unrestricted():
    from api.permissions_admin import _assert_can_grant

    # wildcard caller 可授任意（含 '*'）
    _assert_can_grant({"permission_names": ["*"]}, ["SALARY_READ", "*"])


def test_assert_can_grant_blocks_unheld_permission():
    from api.permissions_admin import _assert_can_grant

    # caller 只有 STUDENTS_READ，卻想授 SALARY_READ → 403（自我提權防線）
    with pytest.raises(HTTPException) as ei:
        _assert_can_grant({"permission_names": ["STUDENTS_READ"]}, ["SALARY_READ"])
    assert ei.value.status_code == 403


def test_assert_can_grant_blocks_wildcard_from_non_wildcard():
    from api.permissions_admin import _assert_can_grant

    # 非 wildcard caller 不可授 '*'
    with pytest.raises(HTTPException) as ei:
        _assert_can_grant({"permission_names": ["STUDENTS_READ"]}, ["*"])
    assert ei.value.status_code == 403


def test_assert_can_grant_allows_held_permission():
    from api.permissions_admin import _assert_can_grant

    # caller 持有 base code（含 scope grant）→ 可授該 code
    _assert_can_grant(
        {"permission_names": ["STUDENTS_READ:own_class"]}, ["STUDENTS_READ:own_class"]
    )


# ── RA-L7：批次加班 employees 上限 ──


def _ot_item():
    return {"employee_id": 1, "hours": 1.0}


def test_batch_overtime_rejects_over_500_employees():
    from api.overtimes import BatchOvertimeCreate

    with pytest.raises(Exception) as ei:  # pydantic ValidationError
        BatchOvertimeCreate(
            overtime_date=date(2026, 6, 2),
            overtime_type="weekday",
            employees=[_ot_item() for _ in range(501)],
        )
    assert "max_length" in str(ei.value) or "at most 500" in str(ei.value).lower()


def test_batch_overtime_accepts_up_to_500():
    from api.overtimes import BatchOvertimeCreate

    m = BatchOvertimeCreate(
        overtime_date=date(2026, 6, 2),
        overtime_type="weekday",
        employees=[_ot_item() for _ in range(500)],
    )
    assert len(m.employees) == 500


# ── RA-L15：uptime-webhook CSRF 豁免（server-to-server token-auth）──


def test_uptime_webhook_in_csrf_exempt():
    from middleware.csrf_origin import CSRF_EXEMPT_PREFIXES

    path = "/api/internal/uptime-webhook"
    assert any(path.startswith(p) for p in CSRF_EXEMPT_PREFIXES)
