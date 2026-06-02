"""RA-HIGH-1：非 scope-aware code 的 :own_class 後綴不得被當全域放行。

漏洞：has_permission 對任意 code 認 `:scope` 後綴 → `SALARY_READ:own_class`
（SALARY_READ 不支援 scope）被當成持有全域 SALARY_READ。本檔守護修法：
只對 canonical SCOPE_AWARE_CODES 認後綴，其餘 code 的 scope 後綴 fail-closed。
"""

from utils.permissions import has_permission, SCOPE_AWARE_CODES, WILDCARD


def test_non_scope_aware_own_class_is_fail_closed():
    # SALARY_READ 不在 scope_options → :own_class 後綴不應授予全域 SALARY_READ
    assert has_permission(["SALARY_READ:own_class"], "SALARY_READ") is False
    assert (
        has_permission(["USER_MANAGEMENT_WRITE:own_class"], "USER_MANAGEMENT_WRITE")
        is False
    )


def test_non_scope_aware_all_suffix_is_fail_closed():
    # 即使是 :all，非 scope-aware code 也不該被後綴授權
    assert has_permission(["SALARY_READ:all"], "SALARY_READ") is False


def test_scope_aware_own_class_still_grants():
    # STUDENTS_READ 是 scope-aware → :own_class 仍視為持有（端點再做 row 過濾）
    assert has_permission(["STUDENTS_READ:own_class"], "STUDENTS_READ") is True


def test_scope_aware_all_still_grants():
    assert has_permission(["PORTFOLIO_WRITE:all"], "PORTFOLIO_WRITE") is True


def test_bare_and_wildcard_unchanged():
    assert has_permission(["SALARY_READ"], "SALARY_READ") is True
    assert has_permission([WILDCARD], "SALARY_READ") is True
    assert has_permission(None, "SALARY_READ") is False


def test_scope_aware_codes_has_exactly_thirteen():
    # canonical 集合與 DB seed（permscope01-04）一致：13 筆
    assert len(SCOPE_AWARE_CODES) == 13
