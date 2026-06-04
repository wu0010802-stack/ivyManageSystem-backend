"""funnel transition 授權 scope regression（QA 2026-06-04 P2-3）。

api/recruitment/funnel.py post_transition 原以 has_permission(user_perms, p)
（bare 存在性）檢查；scoped grant `STUDENTS_WRITE:own_class` 也通過，但 funnel
transition 無班級 context → 自訂角色可轉換任意 visit 的漏斗階段（越權）。
funnel transition 為園級操作，須要求 unrestricted（bare / :all / wildcard）grant。

bare 權限解析為 scope 'all'（resolve_grant 向後相容）→ 正當招生 staff 不受影響。
"""

from api.recruitment.funnel import _missing_unrestricted_permission
from utils.permissions import Permission


def _user(perms):
    return {"role": "staff", "permission_names": perms}


class TestFunnelTransitionUnrestricted:
    def test_bare_grant_allowed(self):
        """招生 staff 持 bare STUDENTS_WRITE → 放行（bare = scope all）。"""
        assert (
            _missing_unrestricted_permission(
                _user(["STUDENTS_WRITE"]), [Permission.STUDENTS_WRITE]
            )
            is None
        )

    def test_all_scope_allowed(self):
        assert (
            _missing_unrestricted_permission(
                _user(["STUDENTS_WRITE:all"]), [Permission.STUDENTS_WRITE]
            )
            is None
        )

    def test_own_class_scope_denied(self):
        """自訂角色 STUDENTS_WRITE:own_class → funnel 無班級 context，須拒絕。"""
        assert (
            _missing_unrestricted_permission(
                _user(["STUDENTS_WRITE:own_class"]), [Permission.STUDENTS_WRITE]
            )
            == Permission.STUDENTS_WRITE
        )

    def test_missing_permission_denied(self):
        assert (
            _missing_unrestricted_permission(_user([]), [Permission.STUDENTS_WRITE])
            == Permission.STUDENTS_WRITE
        )

    def test_all_required_must_be_unrestricted(self):
        """多權限交易（enrolled→deposited 需 CONVERT+STUDENTS_WRITE）缺一即回該 perm。"""
        assert (
            _missing_unrestricted_permission(
                _user(["RECRUITMENT_CONVERT"]),
                [Permission.RECRUITMENT_CONVERT, Permission.STUDENTS_WRITE],
            )
            == Permission.STUDENTS_WRITE
        )
