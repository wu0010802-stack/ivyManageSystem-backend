from utils.permissions import (
    Permission,
    ROLE_TEMPLATES,
    PERMISSION_LABELS,
    has_permission,
)


def test_new_portal_permissions_exist():
    assert Permission.PORTAL_PREVIEW.value == "PORTAL_PREVIEW"
    assert Permission.PORTAL_IMPERSONATE.value == "PORTAL_IMPERSONATE"
    assert "PORTAL_PREVIEW" in PERMISSION_LABELS
    assert "PORTAL_IMPERSONATE" in PERMISSION_LABELS


def test_principal_has_preview_not_impersonate():
    principal_perms = ROLE_TEMPLATES["principal"]
    assert Permission.PORTAL_PREVIEW.value in principal_perms
    assert Permission.PORTAL_IMPERSONATE.value not in principal_perms


def test_admin_wildcard_passes_both():
    admin_perms = ROLE_TEMPLATES["admin"]  # ["*"]
    assert has_permission(admin_perms, Permission.PORTAL_PREVIEW)
    assert has_permission(admin_perms, Permission.PORTAL_IMPERSONATE)
