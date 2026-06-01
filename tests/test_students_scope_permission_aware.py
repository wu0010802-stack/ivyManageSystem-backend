"""Test that api/students.py uses permission-aware scope via code= argument."""

import inspect

import pytest

from utils.portfolio_access import accessible_classroom_ids, is_unrestricted


def test_is_unrestricted_call_path_with_students_read_code():
    """Smoke: passing code=STUDENTS_READ correctly identifies wildcard admin as unrestricted."""
    admin = {"role": "admin", "permission_names": ["*"]}
    assert is_unrestricted(admin, code="STUDENTS_READ") is True


def test_is_unrestricted_with_code_hr_unscoped():
    """hr with bare STUDENTS_READ → unrestricted (bare = :all per resolve_grant)."""
    hr = {"role": "hr", "permission_names": ["STUDENTS_READ"]}
    assert is_unrestricted(hr, code="STUDENTS_READ") is True


def test_is_unrestricted_with_code_user_lacks_perm_returns_false():
    """User without STUDENTS_READ should NOT be unrestricted even if role=hr."""
    hr_no_perm = {"role": "hr", "permission_names": ["DASHBOARD"]}
    # NOTE: in practice this user would be 403'd at endpoint guard; just verifying helper
    assert is_unrestricted(hr_no_perm, code="STUDENTS_READ") is False


def test_call_site_in_api_students_uses_code_param():
    """Regression: ensure api/students.py:get_students passes code= to accessible_classroom_ids."""
    import api.students as students_module

    source = inspect.getsource(students_module.get_students)
    assert (
        "code=Permission.STUDENTS_READ" in source or 'code="STUDENTS_READ"' in source
    ), "get_students should pass code= to accessible_classroom_ids after Task 7 migration"
