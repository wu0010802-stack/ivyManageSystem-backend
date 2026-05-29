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


def test_resolve_grant_two_scoped_invalid_takes_broader():
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
