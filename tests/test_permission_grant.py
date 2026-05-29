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
    user = SimpleNamespace(permission_names=["STUDENTS_READ:bogus_scope"], employee_id=1)
    assert resolve_grant(user, "STUDENTS_READ") is None


from fastapi import HTTPException
from utils.permissions import require_scoped_permission, Permission


def test_require_scoped_permission_returns_user_and_grant():
    user = SimpleNamespace(
        permission_names=["STUDENTS_READ:own_class"],
        employee_id=42,
    )
    dep = require_scoped_permission(Permission.STUDENTS_READ)
    # FastAPI dependency function is the inner callable
    result_user, grant = dep(user=user)
    assert result_user is user
    assert grant.scope == "own_class"


def test_require_scoped_permission_raises_403_when_missing():
    user = SimpleNamespace(permission_names=[], employee_id=1)
    dep = require_scoped_permission(Permission.STUDENTS_READ)
    with pytest.raises(HTTPException) as exc:
        dep(user=user)
    assert exc.value.status_code == 403


def test_require_scoped_permission_wildcard_grants_all():
    user = SimpleNamespace(permission_names=["*"], employee_id=1)
    dep = require_scoped_permission(Permission.STUDENTS_READ)
    _, grant = dep(user=user)
    assert grant.scope == "all"
