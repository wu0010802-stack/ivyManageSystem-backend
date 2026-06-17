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


def test_role_templates_all_use_valid_permission_names():
    """ROLE_TEMPLATES 內每個 perm name 都合法（含 scope-aware :own_class/:all 後綴）。"""
    from utils.permissions import validate_permission_names

    for role, perms in ROLE_TEMPLATES.items():
        invalid = validate_permission_names(perms)
        assert invalid == [], f"ROLE_TEMPLATES[{role}] 含非法 perm: {invalid}"


def test_get_permission_list_wildcard_expands_all():
    expanded = get_permission_list(["*"])
    # 56 條 Permission enum + ROLES_MANAGE ((b) 加) = 57 條成員
    assert len(expanded) == len(list(Permission))
    assert "EMPLOYEES_READ" in expanded
    assert "ROLES_MANAGE" in expanded


def test_get_permission_list_filters_unknown():
    perms = ["EMPLOYEES_READ", "BOGUS_NAME"]
    expanded = get_permission_list(perms)
    assert expanded == ["EMPLOYEES_READ"]


def test_get_permission_list_none_returns_empty():
    assert get_permission_list(None) == []


def test_all_permissions_have_labels():
    """每個 Permission enum 都要有對應的 PERMISSION_LABELS 條目。
    避免「新增 enum 但忘了加 label」造成前端 UI 顯示原始 key。
    """
    from utils.permissions import PERMISSION_LABELS

    for perm in Permission:
        assert perm.value in PERMISSION_LABELS, f"missing label for {perm.value}"


def test_role_templates_principal_inherits_supervisor():
    """principal 必須含 supervisor 全部 + 額外條目。

    原 6 條（SALARY_READ / AUDIT_LOGS / GOV_REPORTS_EXPORT / PORTAL_PREVIEW
    / DATA_QUALITY_READ / DATA_QUALITY_WRITE）+ C13 補齊 3 條教師缺口
    （ANNOUNCEMENTS_READ / DISMISSAL_CALLS_READ / DISMISSAL_CALLS_WRITE），
    使 principal 真正涵蓋 teacher，PORTAL_PREVIEW 越權守衛才不誤擋合法預覽。
    """
    sup_set = set(ROLE_TEMPLATES["supervisor"])
    pri_set = set(ROLE_TEMPLATES["principal"])
    assert sup_set.issubset(pri_set)
    extras = pri_set - sup_set
    assert extras == {
        Permission.SALARY_READ.value,
        Permission.AUDIT_LOGS.value,
        Permission.GOV_REPORTS_EXPORT.value,
        Permission.PORTAL_PREVIEW.value,
        Permission.DATA_QUALITY_READ.value,
        Permission.DATA_QUALITY_WRITE.value,
        Permission.ANNOUNCEMENTS_READ.value,
        Permission.DISMISSAL_CALLS_READ.value,
        Permission.DISMISSAL_CALLS_WRITE.value,
    }


def test_role_templates_principal_excludes_write_and_admin_permissions():
    """principal 不可含 SALARY_WRITE / USER_MANAGEMENT_* / SETTINGS_*。"""
    pri = ROLE_TEMPLATES["principal"]
    assert Permission.SALARY_WRITE.value not in pri
    assert Permission.USER_MANAGEMENT_READ.value not in pri
    assert Permission.USER_MANAGEMENT_WRITE.value not in pri
    assert Permission.SETTINGS_READ.value not in pri
    assert Permission.SETTINGS_WRITE.value not in pri


def test_role_labels_principal_zh():
    """principal 中文 label = 園長。"""
    from utils.permissions import ROLE_LABELS

    assert ROLE_LABELS["principal"] == "園長"


def test_role_templates_accountant_pure_finance():
    """accountant 只含財務 + EMPLOYEES_READ；不可含 EMPLOYEES_WRITE / 考勤 / 學生 / 招生 / 政府匯出。"""
    acc = set(ROLE_TEMPLATES["accountant"])
    forbidden = {
        Permission.EMPLOYEES_WRITE.value,
        Permission.ATTENDANCE_READ.value,
        Permission.ATTENDANCE_WRITE.value,
        Permission.LEAVES_READ.value,
        Permission.STUDENTS_READ.value,
        Permission.RECRUITMENT_READ.value,
        Permission.GOV_REPORTS_EXPORT.value,
        Permission.YEAR_END_FINALIZE.value,
    }
    assert forbidden.isdisjoint(acc), f"accountant 不該含: {forbidden & acc}"


def test_role_templates_accountant_includes_finance_core():
    """accountant 必須含薪資/廠商/學費/年終讀寫 + APPRAISAL_ACCOUNTING。"""
    acc = set(ROLE_TEMPLATES["accountant"])
    required = {
        Permission.EMPLOYEES_READ.value,
        Permission.SALARY_READ.value,
        Permission.SALARY_WRITE.value,
        Permission.FEES_READ.value,
        Permission.FEES_WRITE.value,
        Permission.VENDOR_PAYMENT_READ.value,
        Permission.VENDOR_PAYMENT_WRITE.value,
        Permission.YEAR_END_READ.value,
        Permission.YEAR_END_WRITE.value,
        Permission.APPRAISAL_ACCOUNTING.value,
    }
    assert required.issubset(acc), f"accountant 缺: {required - acc}"


def test_role_labels_accountant_zh():
    from utils.permissions import ROLE_LABELS

    assert ROLE_LABELS["accountant"] == "會計"


def test_role_descriptions_complete():
    """每個 ROLE_TEMPLATES key 都有對應 ROLE_DESCRIPTIONS。"""
    from utils.permissions import ROLE_DESCRIPTIONS

    assert set(ROLE_TEMPLATES.keys()) == set(ROLE_DESCRIPTIONS.keys())


def test_role_descriptions_non_empty():
    """ROLE_DESCRIPTIONS 每個值非空字串。"""
    from utils.permissions import ROLE_DESCRIPTIONS

    for role, desc in ROLE_DESCRIPTIONS.items():
        assert isinstance(desc, str) and len(desc) > 0, f"{role} description 空"


def test_permission_enum_has_roles_manage():
    """ROLES_MANAGE 是 (b) 加的第 57 條 enum，守衛角色/權限定義 CRUD。"""
    assert Permission.ROLES_MANAGE.value == "ROLES_MANAGE"
