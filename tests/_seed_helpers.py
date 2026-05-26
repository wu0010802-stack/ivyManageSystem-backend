"""共享測試 seed helper。

供需要 DB-driven permission 行為的 test 使用，避免重複實作 rolesdb01 migration seed 邏輯。
"""

from __future__ import annotations


def seed_default_permissions_and_roles(session) -> None:
    """模擬 rolesdb01 alembic upgrade 後 permission_definitions + roles 兩表的 seed 結果。

    從 utils.permissions 拉常數，與 migration 內 in-code dict 同源，
    讓 test 不依賴實際跑 alembic 而能拿到等價狀態（SQLite-friendly）。

    必須在 Base.metadata.create_all(engine) 之後呼叫，且 session 須已 bound 到該 engine。
    """
    from models.permission_models import PermissionDefinition, Role
    from utils.permissions import (
        PERMISSION_GROUPS,
        PERMISSION_LABELS,
        ROLE_DESCRIPTIONS,
        ROLE_LABELS,
        ROLE_TEMPLATES,
    )

    # 反查 permission code → group name
    group_lookup: dict[str, str] = {}
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

    # ROLES_MANAGE 是系統分組（手動 patch）
    session.flush()
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
