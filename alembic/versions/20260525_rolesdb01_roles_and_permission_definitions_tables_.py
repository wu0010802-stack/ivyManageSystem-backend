"""roles and permission_definitions tables; seed from in-code dicts

Revision ID: rolesdb01
Revises: mergeheads02
Create Date: 2026-05-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "rolesdb01"
down_revision: Union[str, None] = "mergeheads02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _build_code_to_group_lookup(groups: list) -> dict:
    """反查 PERMISSION_GROUPS：把每個 code 對應的 group_name 拉出。"""
    lookup = {}
    for g in groups:
        name = g["name"]
        for code in g.get("permissions", []) or []:
            lookup[code] = name
        for sp in g.get("split_permissions", []) or []:
            lookup[sp["read"]] = name
            lookup[sp["write"]] = name
    return lookup


def upgrade() -> None:
    op.create_table(
        "permission_definitions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("group_name", sa.Text(), nullable=False, server_default="自訂"),
        sa.Column(
            "is_core", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("code", name="uq_permission_definitions_code"),
    )
    op.create_index(
        "ix_permission_definitions_group", "permission_definitions", ["group_name"]
    )

    op.create_table(
        "roles",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "permissions",
            ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "is_core", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("code", name="uq_roles_code"),
    )

    # Seed permission_definitions（56 + 1 = 57 條 is_core=true）
    from utils.permissions import (
        PERMISSION_LABELS,
        PERMISSION_GROUPS,
        ROLE_TEMPLATES,
        ROLE_LABELS,
        ROLE_DESCRIPTIONS,
    )

    conn = op.get_bind()
    group_lookup = _build_code_to_group_lookup(PERMISSION_GROUPS)

    # ROLES_MANAGE は T1 で PERMISSION_LABELS に追加済み；group_lookup にないので "系統" を明示
    group_lookup["ROLES_MANAGE"] = "系統"

    perm_rows = []
    for code, label in PERMISSION_LABELS.items():
        perm_rows.append(
            {
                "code": code,
                "label": label,
                "description": (
                    "新增/編輯/刪除自訂角色與權限定義"
                    if code == "ROLES_MANAGE"
                    else None
                ),
                "group_name": group_lookup.get(code, "其他"),
                "is_core": True,
            }
        )

    conn.execute(
        sa.text(
            "INSERT INTO permission_definitions (code, label, description, group_name, is_core) "
            "VALUES (:code, :label, :description, :group_name, :is_core)"
        ),
        perm_rows,
    )

    # Seed roles（7 條 is_core=true）
    role_rows = []
    for code, perms in ROLE_TEMPLATES.items():
        role_rows.append(
            {
                "code": code,
                "label": ROLE_LABELS.get(code, code),
                "description": ROLE_DESCRIPTIONS.get(code, ""),
                "permissions": list(perms),
                "is_core": True,
            }
        )

    conn.execute(
        sa.text(
            "INSERT INTO roles (code, label, description, permissions, is_core) "
            "VALUES (:code, :label, :description, :permissions, :is_core)"
        ),
        role_rows,
    )


def downgrade() -> None:
    # 注意：自訂角色與自訂權限資料將丟失（emergency rollback 接受）
    op.drop_table("roles")
    op.drop_index(
        "ix_permission_definitions_group", table_name="permission_definitions"
    )
    op.drop_table("permission_definitions")
