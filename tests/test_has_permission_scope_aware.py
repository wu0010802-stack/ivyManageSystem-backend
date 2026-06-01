"""has_permission scope-suffix aware regression（2026-06-01 latent bug fix）。

Phase 1 + Phase 2.1 ship 後發現：`has_permission(['STUDENTS_READ:own_class'],
'STUDENTS_READ')` 回 False，導致 require_permission 對 :own_class 用戶 403。
本檔守護修法：認 ':scope' 後綴的 entry 持有 base perm。
"""

import pytest
from utils.permissions import has_permission, Permission, WILDCARD


def test_bare_code_unchanged():
    assert has_permission(["STUDENTS_READ"], "STUDENTS_READ") is True


def test_wildcard_passes_all():
    assert has_permission([WILDCARD], "STUDENTS_READ") is True


def test_none_returns_false():
    assert has_permission(None, "STUDENTS_READ") is False


def test_scoped_own_class_recognized_as_having_base_perm():
    assert has_permission(["STUDENTS_READ:own_class"], "STUDENTS_READ") is True


def test_scoped_all_recognized_as_having_base_perm():
    assert has_permission(["STUDENTS_READ:all"], "STUDENTS_READ") is True


def test_scoped_other_perm_does_not_grant():
    assert has_permission(["STUDENTS_WRITE:own_class"], "STUDENTS_READ") is False


def test_substring_collision_avoided():
    # STUDENTS_READ_AUDIT_LOG 不該被當成 STUDENTS_READ_AUDIT 命中
    # 但這裡 base 是 STUDENTS_READ，hold STUDENTS_READ_AUDIT_LOG 不該 grant STUDENTS_READ
    # 因為 split 規則是 ':' 分隔不是 '_' 分隔
    assert has_permission(["STUDENTS_READ_AUDIT_LOG"], "STUDENTS_READ") is False


def test_partial_prefix_match_avoided():
    # PORTFOLIO_READ_FOO（假想）不該被當 PORTFOLIO_READ 命中
    assert has_permission(["PORTFOLIO_READ_FOO"], "PORTFOLIO_READ") is False


def test_permission_enum_input():
    assert has_permission(["STUDENTS_READ:own_class"], Permission.STUDENTS_READ) is True


def test_mixed_permissions():
    perms = ["STUDENTS_READ:own_class", "STUDENTS_HEALTH_WRITE", "DASHBOARD"]
    assert has_permission(perms, "STUDENTS_READ") is True
    assert has_permission(perms, "STUDENTS_HEALTH_WRITE") is True
    assert has_permission(perms, "DASHBOARD") is True
    assert has_permission(perms, "PORTFOLIO_READ") is False
