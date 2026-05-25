"""
接送通知權限設計測試

驗證：
1. DISMISSAL_CALLS_READ / DISMISSAL_CALLS_WRITE 存在於 Permission enum
2. teacher 角色模板包含這兩個權限
3. hr / supervisor 角色模板不包含這兩個權限（portal 非管理端功能）
4. has_permission 對各角色的判斷正確
5. admin（wildcard ["*"]）仍擁有全部權限
"""

import pytest
from utils.permissions import Permission, ROLE_TEMPLATES, WILDCARD, has_permission


class TestDismissalCallsPermissionDefined:
    def test_dismissal_calls_read_exists(self):
        assert hasattr(Permission, "DISMISSAL_CALLS_READ")

    def test_dismissal_calls_write_exists(self):
        assert hasattr(Permission, "DISMISSAL_CALLS_WRITE")

    def test_read_and_write_are_distinct_names(self):
        """READ 與 WRITE 應為不同的權限名稱字串。"""
        assert (
            Permission.DISMISSAL_CALLS_READ.value
            != Permission.DISMISSAL_CALLS_WRITE.value
        )


class TestTeacherRoleTemplateIncludesDismissal:
    def test_teacher_has_dismissal_calls_read(self):
        perms = ROLE_TEMPLATES["teacher"]
        assert has_permission(
            perms, Permission.DISMISSAL_CALLS_READ
        ), "teacher 角色應包含 DISMISSAL_CALLS_READ"

    def test_teacher_has_dismissal_calls_write(self):
        perms = ROLE_TEMPLATES["teacher"]
        assert has_permission(
            perms, Permission.DISMISSAL_CALLS_WRITE
        ), "teacher 角色應包含 DISMISSAL_CALLS_WRITE（需能 acknowledge/complete）"


class TestNonTeacherRolesExcludeDismissal:
    def test_hr_does_not_have_dismissal_calls_read(self):
        """HR 不操作 portal 接送流程"""
        perms = ROLE_TEMPLATES["hr"]
        assert not has_permission(perms, Permission.DISMISSAL_CALLS_READ)

    def test_hr_does_not_have_dismissal_calls_write(self):
        perms = ROLE_TEMPLATES["hr"]
        assert not has_permission(perms, Permission.DISMISSAL_CALLS_WRITE)

    def test_supervisor_does_not_have_dismissal_calls_read(self):
        """supervisor 使用管理端介面，不使用 teacher portal"""
        perms = ROLE_TEMPLATES["supervisor"]
        assert not has_permission(perms, Permission.DISMISSAL_CALLS_READ)

    def test_supervisor_does_not_have_dismissal_calls_write(self):
        perms = ROLE_TEMPLATES["supervisor"]
        assert not has_permission(perms, Permission.DISMISSAL_CALLS_WRITE)


class TestAdminHasAllPermissions:
    def test_admin_has_dismissal_calls_read(self):
        assert has_permission([WILDCARD], Permission.DISMISSAL_CALLS_READ)

    def test_admin_has_dismissal_calls_write(self):
        assert has_permission([WILDCARD], Permission.DISMISSAL_CALLS_WRITE)
