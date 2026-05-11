"""Phase 1 schema and permission tests for MOE reporting module."""

from utils.permissions import (
    Permission,
    PERMISSION_LABELS,
    ROLE_TEMPLATES,
    PERMISSION_GROUPS,
)


def test_gov_reports_view_permission_bit():
    assert Permission.GOV_REPORTS_VIEW.value == 1 << 50


def test_gov_reports_export_permission_bit():
    assert Permission.GOV_REPORTS_EXPORT.value == 1 << 51


def test_gov_reports_permissions_have_labels():
    assert PERMISSION_LABELS["GOV_REPORTS_VIEW"] == "政府申報資料 (檢視)"
    assert PERMISSION_LABELS["GOV_REPORTS_EXPORT"] == "政府申報匯出 (執行)"


def test_admin_role_has_gov_reports_permissions():
    admin_perms = ROLE_TEMPLATES["admin"]
    assert admin_perms & Permission.GOV_REPORTS_VIEW.value
    assert admin_perms & Permission.GOV_REPORTS_EXPORT.value


def test_hr_role_has_gov_reports_view():
    hr_perms = ROLE_TEMPLATES["hr"]
    assert hr_perms & Permission.GOV_REPORTS_VIEW.value


def test_teacher_role_has_no_gov_reports_permissions():
    teacher_perms = ROLE_TEMPLATES["teacher"]
    assert not (teacher_perms & Permission.GOV_REPORTS_VIEW.value)
    assert not (teacher_perms & Permission.GOV_REPORTS_EXPORT.value)
