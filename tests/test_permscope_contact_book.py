"""contact_book scope guard regression（QA 2026-06-04 P2-4）。

api/portal/contact_book.py 的寫入/讀取自班限制原本以角色字串
`current_user.get("role") == "teacher"` 判定 → 非 teacher 自訂角色持
PORTFOLIO_WRITE:own_class 會繞過 _assert_classroom_owned 而寫任意班級聯絡簿。
應改用 is_unrestricted(code=Permission.PORTFOLIO_*.value)，對齊 dismissal_calls。

source-level guard 測試（與 test_permscope_dismissal.py 同模式）。
"""

import inspect


def test_contact_book_scope_uses_is_unrestricted_not_role_string():
    import api.portal.contact_book as mod

    source = inspect.getsource(mod)

    assert "is_unrestricted" in source, "contact_book 應 import 並使用 is_unrestricted"
    assert (
        "code=Permission.PORTFOLIO_WRITE.value" in source
    ), "寫入端點 scope gate 應傳 code=Permission.PORTFOLIO_WRITE.value"
    assert (
        "code=Permission.PORTFOLIO_READ.value" in source
    ), "讀取端點 scope gate 應傳 code=Permission.PORTFOLIO_READ.value"
    # 不應再用角色字串做 scope gate（會漏掉非 teacher 的 scoped 自訂角色）
    assert (
        'get("role") == "teacher"' not in source
    ), "scope gate 不應再以角色字串 role=='teacher' 判定，須改 is_unrestricted"
