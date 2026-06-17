"""tests/test_finance_guards.py — utils/finance_guards 純函式測試。

涵蓋四個 public helpers 與 has_finance_approve 預測式：
- has_finance_approve
- require_not_self_edit
- require_not_self_salary_record
- require_finance_approve
- require_adjustment_reason
"""

import pytest
from fastapi import HTTPException

from utils.finance_guards import (
    EMPLOYEE_SALARY_SENSITIVE_FIELDS,
    FINANCE_APPROVAL_THRESHOLD,
    MIN_FINANCE_REASON_LENGTH,
    has_finance_approve,
    require_adjustment_reason,
    require_finance_approve,
    require_not_self_edit,
    require_not_self_salary_record,
)
from utils.permissions import Permission

# ── has_finance_approve ──────────────────────────────────────────────


class TestHasFinanceApprove:
    def test_with_approve_bit_true(self):
        user = {"permission_names": ["ACTIVITY_PAYMENT_APPROVE"]}
        assert has_finance_approve(user) is True

    def test_without_bit_false(self):
        user = {"permission_names": []}
        assert has_finance_approve(user) is False

    def test_superuser_minus_one_true(self):
        user = {"permission_names": ["*"]}
        assert has_finance_approve(user) is True

    def test_missing_permissions_key_default_zero(self):
        assert has_finance_approve({}) is False


# ── require_not_self_edit ────────────────────────────────────────────


class TestRequireNotSelfEdit:
    def test_self_editing_sensitive_field_raises_403(self):
        user = {"employee_id": 7, "permission_names": []}
        with pytest.raises(HTTPException) as exc:
            require_not_self_edit(user, 7, ["base_salary", "name"])
        assert exc.value.status_code == 403
        assert exc.value.detail["code"] == "SELF_FINANCE_EDIT_FORBIDDEN"
        assert "base_salary" in exc.value.detail["context"]["fields"]

    def test_self_editing_only_non_sensitive_passes(self):
        user = {"employee_id": 7}
        # name / phone 不在敏感清單，應放行
        require_not_self_edit(user, 7, ["name", "phone"])

    def test_pure_admin_no_employee_id_passes(self):
        user = {"employee_id": None, "permission_names": ["*"]}
        # 純管理員可以改任何人的任何欄位
        require_not_self_edit(user, 999, ["base_salary", "hire_date"])

    def test_editing_other_employee_sensitive_field_passes(self):
        user = {"employee_id": 1}
        require_not_self_edit(user, 999, ["base_salary"])

    def test_custom_sensitive_fields_override(self):
        user = {"employee_id": 3}
        with pytest.raises(HTTPException) as exc:
            require_not_self_edit(user, 3, ["title"], sensitive_fields={"title"})
        assert exc.value.status_code == 403

    def test_default_set_contains_known_fields(self):
        assert "base_salary" in EMPLOYEE_SALARY_SENSITIVE_FIELDS
        assert "hire_date" in EMPLOYEE_SALARY_SENSITIVE_FIELDS
        assert "classroom_id" in EMPLOYEE_SALARY_SENSITIVE_FIELDS


# ── require_not_self_salary_record ───────────────────────────────────


class TestRequireNotSelfSalaryRecord:
    def test_self_record_raises_403(self):
        user = {"employee_id": 5}
        with pytest.raises(HTTPException) as exc:
            require_not_self_salary_record(user, 5)
        assert exc.value.status_code == 403
        assert "調整自己的薪資紀錄" in exc.value.detail

    def test_other_record_passes(self):
        user = {"employee_id": 5}
        require_not_self_salary_record(user, 999)

    def test_pure_admin_passes(self):
        user = {"employee_id": None}
        require_not_self_salary_record(user, 5)

    def test_custom_action_label_in_detail(self):
        user = {"employee_id": 5}
        with pytest.raises(HTTPException) as exc:
            require_not_self_salary_record(user, 5, action="退款")
        assert "退款" in exc.value.detail


# ── require_finance_approve ──────────────────────────────────────────


class TestRequireFinanceApprove:
    def test_amount_under_threshold_passes_without_perm(self):
        user = {"permission_names": []}
        # threshold = 1000, amount = 500 → 放行
        require_finance_approve(500, user)

    def test_amount_at_threshold_exactly_requires_approve(self):
        # C10：恰等於閾值（1000）也視為需簽核（>= 才擋），無 approve 權限應 403。
        # Why: 拆筆者可把金額湊成「剛好 = 門檻」鑽 `>` 的漏洞。
        user = {"permission_names": []}
        with pytest.raises(HTTPException) as exc:
            require_finance_approve(FINANCE_APPROVAL_THRESHOLD, user)
        assert exc.value.status_code == 403

    def test_amount_at_threshold_with_perm_passes(self):
        user = {"permission_names": ["ACTIVITY_PAYMENT_APPROVE"]}
        require_finance_approve(FINANCE_APPROVAL_THRESHOLD, user)

    def test_amount_just_below_threshold_passes(self):
        # 999 < 1000 → 仍放行
        user = {"permission_names": []}
        require_finance_approve(FINANCE_APPROVAL_THRESHOLD - 1, user)

    def test_amount_over_threshold_without_perm_raises(self):
        user = {"permission_names": []}
        with pytest.raises(HTTPException) as exc:
            require_finance_approve(5000, user, action_label="退費")
        assert exc.value.status_code == 403
        assert "退費" in exc.value.detail
        assert "NT$5,000" in exc.value.detail

    def test_amount_over_threshold_with_perm_passes(self):
        user = {"permission_names": ["ACTIVITY_PAYMENT_APPROVE"]}
        require_finance_approve(99999, user)

    def test_custom_threshold(self):
        user = {"permission_names": []}
        # 自訂 threshold=100，amount=200 應擋
        with pytest.raises(HTTPException):
            require_finance_approve(200, user, threshold=100)
        # 同金額但 threshold=300 應放行
        require_finance_approve(200, user, threshold=300)


# ── require_adjustment_reason ────────────────────────────────────────


class TestRequireAdjustmentReason:
    def test_valid_reason_returns_cleaned(self):
        result = require_adjustment_reason("  董事會核准之獎金調整  ")
        assert result == "董事會核准之獎金調整"

    def test_none_raises_400(self):
        with pytest.raises(HTTPException) as exc:
            require_adjustment_reason(None)
        assert exc.value.status_code == 400
        assert "原因" in exc.value.detail

    def test_empty_string_raises(self):
        with pytest.raises(HTTPException):
            require_adjustment_reason("")

    def test_whitespace_only_raises(self):
        with pytest.raises(HTTPException):
            require_adjustment_reason("    ")

    def test_too_short_raises(self):
        # MIN = 5; 給 4 個字應擋
        with pytest.raises(HTTPException) as exc:
            require_adjustment_reason("一二三四")
        assert exc.value.status_code == 400

    def test_exactly_min_length_passes(self):
        # 剛好 5 字應放行
        result = require_adjustment_reason("一二三四五")
        assert result == "一二三四五"
        assert len(result) == MIN_FINANCE_REASON_LENGTH

    def test_custom_min_length(self):
        with pytest.raises(HTTPException):
            require_adjustment_reason("a" * 9, min_length=10)
        assert require_adjustment_reason("a" * 10, min_length=10) == "a" * 10
