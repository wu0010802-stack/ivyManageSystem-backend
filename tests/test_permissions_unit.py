"""Unit tests for utils/permissions.py - new string-based API."""

from utils.permissions import (
    Permission,
    WILDCARD,
    LEGACY_PERMISSION_BITS,
    ROLE_TEMPLATES,
    has_permission,
    resolve_user_permissions,
    get_role_default_permissions,
    get_permission_list,
    get_permissions_definition,
)


def test_permission_enum_inherits_str():
    """Permission.X.value should be the same string."""
    assert Permission.EMPLOYEES_READ.value == "EMPLOYEES_READ"
    assert Permission.VENDOR_PAYMENT_WRITE.value == "VENDOR_PAYMENT_WRITE"
    # Inherits str
    assert isinstance(Permission.DASHBOARD, str)


def test_legacy_bits_snapshot_has_63_entries():
    """凍結快照：恰好 63 條，與重構前 enum 數量對齊。"""
    assert len(LEGACY_PERMISSION_BITS) == 63
    # 每條都對應一個 Permission enum 值
    for name in LEGACY_PERMISSION_BITS:
        assert name in Permission.__members__


def test_legacy_bits_no_duplicate_bits():
    """所有 bit 值唯一。"""
    bits = list(LEGACY_PERMISSION_BITS.values())
    assert len(bits) == len(set(bits))


def test_legacy_bits_max_bit_is_62():
    """重構前最高位元 1<<62 (VENDOR_PAYMENT_WRITE)。"""
    assert max(LEGACY_PERMISSION_BITS.values()) == (1 << 62)


def test_has_permission_wildcard():
    assert has_permission(["*"], Permission.EMPLOYEES_READ) is True
    assert has_permission(["*"], "ANY_STRING") is True


def test_has_permission_hit():
    perms = ["EMPLOYEES_READ", "SALARY_WRITE"]
    assert has_permission(perms, Permission.EMPLOYEES_READ) is True
    assert has_permission(perms, "SALARY_WRITE") is True


def test_has_permission_miss():
    perms = ["EMPLOYEES_READ"]
    assert has_permission(perms, Permission.SALARY_WRITE) is False


def test_has_permission_none_input():
    """None 視為無權限。caller 必須先 resolve_user_permissions。"""
    assert has_permission(None, Permission.EMPLOYEES_READ) is False


def test_has_permission_empty_list():
    assert has_permission([], Permission.EMPLOYEES_READ) is False


def test_has_permission_accepts_str_or_enum():
    perms = ["EMPLOYEES_READ"]
    assert has_permission(perms, "EMPLOYEES_READ") is True
    assert has_permission(perms, Permission.EMPLOYEES_READ) is True


class _FakeUser:
    """Stand-in for SQLAlchemy User model in unit tests."""

    def __init__(self, role: str, permission_names):
        self.role = role
        self.permission_names = permission_names


def test_resolve_uses_role_default_when_null():
    u = _FakeUser(role="hr", permission_names=None)
    perms = resolve_user_permissions(u)
    assert "EMPLOYEES_READ" in perms
    assert "SALARY_READ" in perms


def test_resolve_returns_explicit_when_set():
    u = _FakeUser(role="hr", permission_names=["ONLY_ONE_PERM"])
    perms = resolve_user_permissions(u)
    assert perms == ["ONLY_ONE_PERM"]


def test_resolve_admin_role_default_is_wildcard():
    u = _FakeUser(role="admin", permission_names=None)
    perms = resolve_user_permissions(u)
    assert "*" in perms


def test_resolve_parent_role_default_is_empty():
    u = _FakeUser(role="parent", permission_names=None)
    perms = resolve_user_permissions(u)
    assert perms == []


def test_get_role_default_unknown_role_falls_back_to_teacher():
    """未知角色 fallback 為 teacher 預設。"""
    perms = get_role_default_permissions("xxxxx")
    assert perms == get_role_default_permissions("teacher")


def test_role_templates_all_use_valid_permission_names():
    """ROLE_TEMPLATES 內每個 perm name 都在 Permission enum 中（或 wildcard）。"""
    for role, perms in ROLE_TEMPLATES.items():
        for p in perms:
            assert (
                p == WILDCARD or p in Permission.__members__
            ), f"ROLE_TEMPLATES[{role}] 含非法 perm: {p}"


def test_get_permission_list_wildcard_expands_all():
    expanded = get_permission_list(["*"])
    assert len(expanded) == 63
    assert "EMPLOYEES_READ" in expanded


def test_get_permission_list_filters_unknown():
    perms = ["EMPLOYEES_READ", "BOGUS_NAME"]
    expanded = get_permission_list(perms)
    assert expanded == ["EMPLOYEES_READ"]


def test_get_permission_list_none_returns_empty():
    assert get_permission_list(None) == []


def test_get_permissions_definition_shape():
    defn = get_permissions_definition()
    assert "permissions" in defn
    assert "groups" in defn
    assert "roles" in defn
    assert "split_modules" in defn
    # value 應為字串（與 name 相同），不再是 int
    assert defn["permissions"]["EMPLOYEES_READ"]["value"] == "EMPLOYEES_READ"
    assert defn["permissions"]["EMPLOYEES_READ"]["label"] == "員工管理 (檢視)"


def test_get_permissions_definition_admin_role_is_wildcard():
    defn = get_permissions_definition()
    assert defn["roles"]["admin"]["permissions"] == ["*"]


def test_all_permissions_have_labels():
    """每個 Permission enum 都要有對應的 PERMISSION_LABELS 條目。
    避免「新增 enum 但忘了加 label」造成前端 UI 顯示原始 key。
    """
    from utils.permissions import PERMISSION_LABELS

    for perm in Permission:
        assert perm.value in PERMISSION_LABELS, f"missing label for {perm.value}"
