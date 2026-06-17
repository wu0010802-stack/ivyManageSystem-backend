# tests/test_permission_grant.py
import pytest
from types import SimpleNamespace
from utils.permissions import resolve_grant, PermissionGrant


def _user(*perms):
    return SimpleNamespace(permission_names=list(perms), employee_id=1)


def test_resolve_grant_wildcard_returns_all_scope():
    g = resolve_grant(_user("*"), "STUDENTS_READ")
    assert g == PermissionGrant("STUDENTS_READ", "all")


def test_resolve_grant_bare_code_returns_all_scope():
    g = resolve_grant(_user("STUDENTS_READ"), "STUDENTS_READ")
    assert g == PermissionGrant("STUDENTS_READ", "all")


def test_resolve_grant_scoped_code_returns_scope():
    g = resolve_grant(_user("STUDENTS_READ:own_class"), "STUDENTS_READ")
    assert g == PermissionGrant("STUDENTS_READ", "own_class")


def test_resolve_grant_not_held_returns_none():
    g = resolve_grant(_user("DASHBOARD"), "STUDENTS_READ")
    assert g is None


def test_resolve_grant_bare_and_scoped_takes_broader():
    # If user has both, the broader (all) wins
    g = resolve_grant(
        _user("STUDENTS_READ", "STUDENTS_READ:own_class"), "STUDENTS_READ"
    )
    assert g.scope == "all"


def test_resolve_grant_two_scoped_takes_broader():
    g = resolve_grant(
        _user("STUDENTS_READ:own_class", "STUDENTS_READ:all"), "STUDENTS_READ"
    )
    assert g.scope == "all"


def test_resolve_grant_empty_permission_names():
    user = SimpleNamespace(permission_names=[], employee_id=1)
    assert resolve_grant(user, "STUDENTS_READ") is None


def test_resolve_grant_none_permission_names():
    user = SimpleNamespace(permission_names=None, employee_id=1)
    assert resolve_grant(user, "STUDENTS_READ") is None


def test_resolve_grant_unknown_scope_only_returns_none():
    """User with only an invalid scope string falls fail-closed to None (no silent upgrade)."""
    user = SimpleNamespace(
        permission_names=["STUDENTS_READ:bogus_scope"], employee_id=1
    )
    assert resolve_grant(user, "STUDENTS_READ") is None


import logging
from utils.permissions import check_scope_options_sanity


def test_sanity_warns_when_students_prefix_lacks_scope_options(caplog):
    seed = {"STUDENTS_READ": None, "DASHBOARD": None}
    with caplog.at_level(logging.WARNING):
        check_scope_options_sanity(seed)
    assert any("STUDENTS_READ" in r.message for r in caplog.records)
    # DASHBOARD lacks STUDENTS_/PORTFOLIO_/etc prefix -> no warning
    assert not any("DASHBOARD" in r.message for r in caplog.records)


def test_sanity_no_warning_when_scope_options_present(caplog):
    seed = {"STUDENTS_READ": ["own_class", "all"]}
    with caplog.at_level(logging.WARNING):
        check_scope_options_sanity(seed)
    assert len(caplog.records) == 0


# --- dict input tests (get_current_user returns a dict, not a model) ---


def test_resolve_grant_dict_wildcard_returns_all():
    user = {"permission_names": ["*"], "employee_id": 1}
    g = resolve_grant(user, "STUDENTS_READ")
    assert g == PermissionGrant("STUDENTS_READ", "all")


def test_resolve_grant_dict_scoped_own_class():
    user = {"permission_names": ["STUDENTS_READ:own_class"], "employee_id": 1}
    g = resolve_grant(user, "STUDENTS_READ")
    assert g == PermissionGrant("STUDENTS_READ", "own_class")


def test_resolve_grant_dict_empty_permission_names():
    user = {"permission_names": [], "employee_id": 1}
    assert resolve_grant(user, "STUDENTS_READ") is None


def test_resolve_grant_dict_missing_key():
    user = {"employee_id": 1}
    assert resolve_grant(user, "STUDENTS_READ") is None


def test_resolve_grant_dict_none_permission_names():
    user = {"permission_names": None, "employee_id": 1}
    assert resolve_grant(user, "STUDENTS_READ") is None
