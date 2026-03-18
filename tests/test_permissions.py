"""
tests/test_permissions.py — 權限位元運算整合測試

測試範圍：
- Permission IntFlag 位元唯一性與讀寫分離
- has_permission() / get_permission_list() 語意正確性
- ROLE_TEMPLATES 內容驗證
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.permissions import (
    Permission,
    ROLE_TEMPLATES,
    _RW_PAIRS,
    has_permission,
    get_permission_list,
    get_role_default_permissions,
    PERMISSION_LABELS,
)


# ---------------------------------------------------------------------------
# 位元定義完整性
# ---------------------------------------------------------------------------

class TestPermissionBitDefinitions:
    def test_no_duplicate_bit_positions(self):
        """每個 Permission 成員必須有唯一的位元值（不含 ALL）。"""
        values = [p.value for p in Permission if p != Permission.ALL]
        assert len(values) == len(set(values)), "有重複的位元值"

    def test_read_and_write_are_distinct_bits(self):
        """每對讀寫權限的位元不可重疊。"""
        for read_p, write_p in _RW_PAIRS:
            assert (read_p & write_p) == 0, (
                f"{read_p.name} 與 {write_p.name} 位元重疊"
            )

    def test_read_lower_than_write_for_split_modules(self):
        """讀取權限的位元值應低於對應的寫入權限（低位 READ，高位 WRITE）。"""
        for read_p, write_p in _RW_PAIRS:
            assert read_p.value < write_p.value, (
                f"{read_p.name}={read_p.value} 不低於 {write_p.name}={write_p.value}"
            )

    def test_all_permissions_have_labels(self):
        """每個 Permission 成員（不含 ALL）都應有對應的中文標籤。"""
        for perm in Permission:
            if perm == Permission.ALL:
                continue
            assert perm.name in PERMISSION_LABELS, f"{perm.name} 缺少標籤"


# ---------------------------------------------------------------------------
# has_permission()
# ---------------------------------------------------------------------------

class TestHasPermission:
    def test_admin_minus_one_has_all_permissions(self):
        """-1（管理員）應對任何權限回傳 True。"""
        for perm in Permission:
            if perm == Permission.ALL:
                continue
            assert has_permission(-1, perm), f"admin 應擁有 {perm.name}"

    def test_exact_permission_returns_true(self):
        """恰好擁有該權限時回傳 True。"""
        mask = Permission.STUDENTS_READ.value
        assert has_permission(mask, Permission.STUDENTS_READ)

    def test_missing_permission_returns_false(self):
        """未擁有的權限應回傳 False。"""
        mask = Permission.STUDENTS_READ.value
        assert not has_permission(mask, Permission.STUDENTS_WRITE)

    def test_combined_mask_contains_all_included_permissions(self):
        """位元 OR 組合的遮罩應含全部加入的權限。"""
        mask = (
            Permission.SALARY_READ.value
            | Permission.SALARY_WRITE.value
            | Permission.EMPLOYEES_READ.value
        )
        assert has_permission(mask, Permission.SALARY_READ)
        assert has_permission(mask, Permission.SALARY_WRITE)
        assert has_permission(mask, Permission.EMPLOYEES_READ)
        assert not has_permission(mask, Permission.EMPLOYEES_WRITE)

    def test_zero_mask_has_no_permissions(self):
        """遮罩為 0 不應擁有任何權限。"""
        for perm in Permission:
            if perm == Permission.ALL:
                continue
            assert not has_permission(0, perm)


# ---------------------------------------------------------------------------
# get_permission_list()
# ---------------------------------------------------------------------------

class TestGetPermissionList:
    def test_minus_one_returns_all_labels(self):
        """-1 應回傳所有已知權限名稱。"""
        result = get_permission_list(-1)
        for key in PERMISSION_LABELS:
            assert key in result, f"{key} 應在 -1 遮罩的結果中"

    def test_single_permission_mask(self):
        """單一位元遮罩只應含對應的一個名稱。"""
        mask = Permission.CLASSROOMS_WRITE.value
        result = get_permission_list(mask)
        assert "CLASSROOMS_WRITE" in result
        assert "CLASSROOMS_READ" not in result

    def test_combined_mask_returns_exact_set(self):
        """複合遮罩應精確回傳包含的名稱。"""
        mask = Permission.LEAVES_READ.value | Permission.OVERTIME_READ.value
        result = get_permission_list(mask)
        assert "LEAVES_READ" in result
        assert "OVERTIME_READ" in result
        assert "LEAVES_WRITE" not in result
        assert "OVERTIME_WRITE" not in result

    def test_zero_mask_returns_empty_list(self):
        """遮罩 0 應回傳空列表。"""
        assert get_permission_list(0) == []


# ---------------------------------------------------------------------------
# ROLE_TEMPLATES 與 get_role_default_permissions()
# ---------------------------------------------------------------------------

class TestRoleTemplates:
    def test_admin_role_is_minus_one(self):
        """admin 角色應為 -1（全部權限）。"""
        assert ROLE_TEMPLATES["admin"] == -1

    def test_teacher_role_has_exactly_expected_permissions(self):
        """teacher 角色應只有 DASHBOARD、CALENDAR、ANNOUNCEMENTS_READ、
        DISMISSAL_CALLS_READ、DISMISSAL_CALLS_WRITE，不含任何管理權限。"""
        teacher_mask = ROLE_TEMPLATES["teacher"]
        required = {
            Permission.DASHBOARD,
            Permission.CALENDAR,
            Permission.ANNOUNCEMENTS_READ,
            Permission.DISMISSAL_CALLS_READ,
            Permission.DISMISSAL_CALLS_WRITE,
        }
        for perm in required:
            assert has_permission(teacher_mask, perm), (
                f"teacher 應擁有 {perm.name}"
            )
        # 不應有管理類寫入權限
        forbidden = [
            Permission.STUDENTS_WRITE,
            Permission.CLASSROOMS_WRITE,
            Permission.SALARY_WRITE,
            Permission.EMPLOYEES_WRITE,
        ]
        for perm in forbidden:
            assert not has_permission(teacher_mask, perm), (
                f"teacher 不應擁有 {perm.name}"
            )

    def test_hr_role_has_salary_and_attendance(self):
        """hr 角色應含薪資與出勤管理的讀寫權限。"""
        mask = ROLE_TEMPLATES["hr"]
        for perm in [
            Permission.SALARY_READ, Permission.SALARY_WRITE,
            Permission.ATTENDANCE_READ, Permission.ATTENDANCE_WRITE,
            Permission.EMPLOYEES_READ, Permission.EMPLOYEES_WRITE,
        ]:
            assert has_permission(mask, perm), f"hr 應擁有 {perm.name}"

    def test_supervisor_role_has_classroom_and_student(self):
        """supervisor 角色應含學生與班級管理的讀寫權限。"""
        mask = ROLE_TEMPLATES["supervisor"]
        for perm in [
            Permission.STUDENTS_READ, Permission.STUDENTS_WRITE,
            Permission.CLASSROOMS_READ, Permission.CLASSROOMS_WRITE,
        ]:
            assert has_permission(mask, perm), f"supervisor 應擁有 {perm.name}"

    def test_get_role_default_permissions_returns_template(self):
        """get_role_default_permissions 應與 ROLE_TEMPLATES 一致。"""
        for role in ("admin", "hr", "supervisor", "teacher"):
            assert get_role_default_permissions(role) == ROLE_TEMPLATES[role]

    def test_unknown_role_falls_back_to_teacher(self):
        """未知角色預設應回傳 teacher 的權限值。"""
        result = get_role_default_permissions("unknown_role")
        assert result == ROLE_TEMPLATES["teacher"]
