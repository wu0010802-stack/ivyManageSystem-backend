"""驗證 rolesdb01 alembic upgrade 之後 permission_definitions 與 roles 兩表的 seed 內容。

用 SQLite + Base.metadata.create_all + 手動 seed（不依賴 alembic upgrade，避免 PG-only DDL）。
"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base
from models.permission_models import PermissionDefinition, Role


@pytest.fixture
def session_with_seed(tmp_path):
    """建立隔離 SQLite session + 跑 Base.metadata.create_all + 手動 seed 模擬 rolesdb01 結果。"""
    db_path = tmp_path / "perm-db-seed.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sessionmaker(bind=engine)

    Base.metadata.create_all(engine)

    Session = sessionmaker(bind=engine)
    session = Session()

    # 模擬 rolesdb01 seed 內容（與 migration 內 in-code dict import 一致）
    from utils.permissions import (
        PERMISSION_LABELS,
        PERMISSION_GROUPS,
        ROLE_TEMPLATES,
        ROLE_LABELS,
        ROLE_DESCRIPTIONS,
    )

    # 反查 group lookup
    group_lookup = {}
    for g in PERMISSION_GROUPS:
        for code in g.get("permissions", []) or []:
            group_lookup[code] = g["name"]
        for sp in g.get("split_permissions", []) or []:
            group_lookup[sp["read"]] = g["name"]
            group_lookup[sp["write"]] = g["name"]

    for code, label in PERMISSION_LABELS.items():
        session.add(
            PermissionDefinition(
                code=code,
                label=label,
                description=None,
                group_name=group_lookup.get(code, "其他"),
                is_core=True,
            )
        )
    # ROLES_MANAGE 已在 T1 加進 PERMISSION_LABELS，上面 loop 已 seed。
    # 但若 group_lookup 沒它，會被歸到「其他」。手動 patch 讓它在「系統」分組：
    rm = session.query(PermissionDefinition).filter_by(code="ROLES_MANAGE").first()
    if rm is not None:
        rm.group_name = "系統"
        rm.description = "新增/編輯/刪除自訂角色與權限定義"

    for code, perms in ROLE_TEMPLATES.items():
        session.add(
            Role(
                code=code,
                label=ROLE_LABELS.get(code, code),
                description=ROLE_DESCRIPTIONS.get(code, ""),
                permissions=list(perms),
                is_core=True,
            )
        )
    session.commit()

    yield session
    session.close()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_seed_permission_definitions_count(session_with_seed):
    """seed 後 permission_definitions 應有 PERMISSION_LABELS 全部條目（含 ROLES_MANAGE）為 is_core=true。"""
    from utils.permissions import PERMISSION_LABELS

    count = (
        session_with_seed.query(PermissionDefinition).filter_by(is_core=True).count()
    )
    assert count == len(PERMISSION_LABELS)


def test_seed_permission_definitions_roles_manage_exists(session_with_seed):
    pd = (
        session_with_seed.query(PermissionDefinition)
        .filter_by(code="ROLES_MANAGE")
        .first()
    )
    assert pd is not None
    assert pd.is_core is True
    assert pd.label == "角色與權限管理"
    assert pd.group_name == "系統"


def test_seed_roles_count(session_with_seed):
    """seed 後 roles 應有 7 條 is_core=true（含 (a) 加的 principal/accountant）。"""
    count = session_with_seed.query(Role).filter_by(is_core=True).count()
    assert count == 7


def test_seed_roles_admin_is_wildcard(session_with_seed):
    admin_role = session_with_seed.query(Role).filter_by(code="admin").first()
    assert admin_role.permissions == ["*"]


def test_seed_roles_principal_inherits_supervisor(session_with_seed):
    """principal seed 內容 = supervisor + SALARY_READ + AUDIT_LOGS + GOV_REPORTS_EXPORT
    + DATA_QUALITY_READ + DATA_QUALITY_WRITE。"""
    from utils.permissions import ROLE_TEMPLATES, Permission

    pri = set(
        session_with_seed.query(Role).filter_by(code="principal").first().permissions
    )
    sup = set(ROLE_TEMPLATES["supervisor"])
    assert sup.issubset(pri)
    extras = pri - sup
    assert extras == {
        Permission.SALARY_READ.value,
        Permission.AUDIT_LOGS.value,
        Permission.GOV_REPORTS_EXPORT.value,
        Permission.DATA_QUALITY_READ.value,
        Permission.DATA_QUALITY_WRITE.value,
    }


# ============================================================
# T5: get_permissions_definition(session) 從 DB 拉
# ============================================================


def test_get_permissions_definition_from_db_returns_all_roles(session_with_seed):
    """get_permissions_definition(session) 應回 7 個 role，每個含 label/description/permissions/is_core。"""
    from utils.permissions import get_permissions_definition

    definition = get_permissions_definition(session_with_seed)
    roles = definition["roles"]
    assert set(roles.keys()) == {
        "admin",
        "principal",
        "supervisor",
        "hr",
        "accountant",
        "teacher",
        "parent",
    }
    for role_data in roles.values():
        assert "label" in role_data
        assert "description" in role_data
        assert "permissions" in role_data
        assert "is_core" in role_data
        assert role_data["is_core"] is True


def test_get_permissions_definition_from_db_returns_all_permissions(session_with_seed):
    """get_permissions_definition(session).permissions 應含 PERMISSION_LABELS 全部條目（含 ROLES_MANAGE）。"""
    from utils.permissions import get_permissions_definition, PERMISSION_LABELS

    definition = get_permissions_definition(session_with_seed)
    perms = definition["permissions"]
    assert len(perms) == len(PERMISSION_LABELS)
    assert "ROLES_MANAGE" in perms
    for perm_data in perms.values():
        assert "value" in perm_data
        assert "label" in perm_data
        assert "is_core" in perm_data


def test_get_permissions_definition_includes_groups(session_with_seed):
    """response 應含 groups（從 group_name 動態組）。"""
    from utils.permissions import get_permissions_definition

    definition = get_permissions_definition(session_with_seed)
    assert "groups" in definition
    assert len(definition["groups"]) > 0


def test_get_permissions_definition_includes_split_modules(session_with_seed):
    """split_modules 暫保 in-code，仍在 response 內。"""
    from utils.permissions import get_permissions_definition, SPLIT_MODULES

    definition = get_permissions_definition(session_with_seed)
    assert definition["split_modules"] == SPLIT_MODULES


# ============================================================
# T6: get_role_default_permissions(session, code) 從 DB 拉
# ============================================================


def test_get_role_default_permissions_returns_db_role(session_with_seed):
    from utils.permissions import get_role_default_permissions

    perms = get_role_default_permissions(session_with_seed, "principal")
    # principal = supervisor 全部 + SALARY_READ + AUDIT_LOGS + GOV_REPORTS_EXPORT
    assert "SALARY_READ" in perms
    assert "AUDIT_LOGS" in perms
    assert "GOV_REPORTS_EXPORT" in perms


def test_get_role_default_permissions_admin_wildcard(session_with_seed):
    from utils.permissions import get_role_default_permissions

    assert get_role_default_permissions(session_with_seed, "admin") == ["*"]


def test_get_role_default_permissions_unknown_role_falls_back_to_teacher(
    session_with_seed,
):
    """未知 role code 回傳 teacher 預設（既有行為）。"""
    from utils.permissions import get_role_default_permissions

    perms = get_role_default_permissions(session_with_seed, "nonexistent_role_xyz")
    teacher_perms = get_role_default_permissions(session_with_seed, "teacher")
    assert perms == teacher_perms
