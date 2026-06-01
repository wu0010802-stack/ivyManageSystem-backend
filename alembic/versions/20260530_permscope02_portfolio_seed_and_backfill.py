"""PORTFOLIO_* scope_options seed + teacher backfill (Phase 2.1)

Revision ID: permscope02
Revises: permscope01
Create Date: 2026-05-30

本 migration 將 3 個 PORTFOLIO_* permission code 標記為 scope-aware，
並把現有 teacher role / teacher users 的 bare PORTFOLIO_* 升級為 :own_class。

依賴 permscope01 已新增的 permission_definitions.scope_options 欄位，
本 migration 不再 add column；downgrade 也不 drop column（屬 permscope01 管轄）。

downgrade 範圍只限 PORTFOLIO_*，不會碰到 STUDENTS_*:own_class（permscope01 管轄）。
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision = "permscope02"
down_revision: Union[str, Sequence[str], None] = "permscope01"
branch_labels = None
depends_on = None

SCOPE_AWARE_CODES = (
    "PORTFOLIO_READ",
    "PORTFOLIO_WRITE",
    "PORTFOLIO_PUBLISH",
)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    codes_sql = ", ".join(f"'{c}'" for c in SCOPE_AWARE_CODES)

    # 1. seed：3 個 PORTFOLIO_* codes 標記為 scope-aware
    if dialect == "postgresql":
        op.execute(f"""
            UPDATE permission_definitions
            SET scope_options = ARRAY['own_class','all']
            WHERE code IN ({codes_sql})
        """)
    else:
        op.execute(f"""
            UPDATE permission_definitions
            SET scope_options = '["own_class","all"]'
            WHERE code IN ({codes_sql})
        """)

    # 2. teacher role：bare PORTFOLIO_* → :own_class（只改 code='teacher' AND is_core=true）
    if dialect == "postgresql":
        op.execute(sa.text(f"""
            UPDATE roles
            SET permissions = ARRAY(
                SELECT CASE
                    WHEN p IN ({codes_sql}) THEN p || :suffix
                    ELSE p
                END
                FROM unnest(permissions) AS p
            )
            WHERE code = 'teacher' AND is_core = true
        """).bindparams(suffix=":own_class"))
    else:
        rows = bind.execute(
            sa.text(
                "SELECT id, permissions FROM roles WHERE code='teacher' AND is_core=1"
            )
        ).fetchall()
        for rid, perms_json in rows:
            perms = (
                json.loads(perms_json) if isinstance(perms_json, str) else perms_json
            )
            new_perms = [
                f"{p}:own_class" if p in SCOPE_AWARE_CODES else p for p in perms
            ]
            bind.execute(
                sa.text("UPDATE roles SET permissions=:p WHERE id=:i"),
                {"p": json.dumps(new_perms), "i": rid},
            )

    # 3. teacher users：bare PORTFOLIO_* → :own_class + bump token_version
    #    （跳過 wildcard '*' 持有者；其他 code（含 STUDENTS_*:own_class）原樣保留）
    if dialect == "postgresql":
        op.execute(sa.text(f"""
            UPDATE users
            SET permission_names = ARRAY(
                SELECT CASE
                    WHEN p IN ({codes_sql}) THEN p || :suffix
                    ELSE p
                END
                FROM unnest(permission_names) AS p
            ),
            token_version = COALESCE(token_version, 0) + 1
            WHERE role = 'teacher'
              AND NOT ('*' = ANY(permission_names))
        """).bindparams(suffix=":own_class"))
    else:
        rows = bind.execute(
            sa.text(
                "SELECT id, permission_names, token_version FROM users WHERE role='teacher'"
            )
        ).fetchall()
        for uid, names_json, tv in rows:
            names = (
                json.loads(names_json) if isinstance(names_json, str) else names_json
            )
            if "*" in names:
                continue
            new_names = [
                f"{n}:own_class" if n in SCOPE_AWARE_CODES else n for n in names
            ]
            bind.execute(
                sa.text(
                    "UPDATE users SET permission_names=:n, token_version=:t WHERE id=:i"
                ),
                {"n": json.dumps(new_names), "t": (tv or 0) + 1, "i": uid},
            )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # downgrade 只剝 PORTFOLIO_* 後綴，不可動 STUDENTS_*:own_class（屬 permscope01 管轄）
    scoped_portfolio_codes = tuple(f"{c}:own_class" for c in SCOPE_AWARE_CODES)
    codes_sql = ", ".join(f"'{c}'" for c in SCOPE_AWARE_CODES)
    scoped_sql = ", ".join(f"'{c}'" for c in scoped_portfolio_codes)

    if dialect == "postgresql":
        # 1. teacher users：只剝 PORTFOLIO_*:own_class，其他 :own_class 保留
        op.execute(f"""
            UPDATE users
            SET permission_names = ARRAY(
                SELECT CASE
                    WHEN p IN ({scoped_sql}) THEN split_part(p, ':', 1)
                    ELSE p
                END
                FROM unnest(permission_names) AS p
            ),
            token_version = COALESCE(token_version, 0) + 1
            WHERE role = 'teacher'
        """)
        # 2. teacher role：只剝 PORTFOLIO_*:own_class
        op.execute(f"""
            UPDATE roles
            SET permissions = ARRAY(
                SELECT CASE
                    WHEN p IN ({scoped_sql}) THEN split_part(p, ':', 1)
                    ELSE p
                END
                FROM unnest(permissions) AS p
            )
            WHERE code = 'teacher' AND is_core = true
        """)
        # 3. unseed scope_options on PORTFOLIO_* codes
        op.execute(f"""
            UPDATE permission_definitions
            SET scope_options = NULL
            WHERE code IN ({codes_sql})
        """)
    else:
        # SQLite：以 Python 轉換，只剝 base_code 在 SCOPE_AWARE_CODES 的 suffix
        # 1. teacher users
        rows = bind.execute(
            sa.text(
                "SELECT id, permission_names, token_version FROM users WHERE role='teacher'"
            )
        ).fetchall()
        for uid, names_json, tv in rows:
            items = (
                json.loads(names_json) if isinstance(names_json, str) else names_json
            )
            new_items = []
            for p in items:
                base = p.split(":", 1)[0]
                if ":" in p and base in SCOPE_AWARE_CODES:
                    new_items.append(base)
                else:
                    new_items.append(p)
            bind.execute(
                sa.text(
                    "UPDATE users SET permission_names=:v, token_version=:t WHERE id=:i"
                ),
                {"v": json.dumps(new_items), "t": (tv or 0) + 1, "i": uid},
            )
        # 2. teacher role
        rows = bind.execute(
            sa.text(
                "SELECT id, permissions FROM roles WHERE code='teacher' AND is_core=1"
            )
        ).fetchall()
        for rid, val in rows:
            items = json.loads(val) if isinstance(val, str) else val
            new_items = []
            for p in items:
                base = p.split(":", 1)[0]
                if ":" in p and base in SCOPE_AWARE_CODES:
                    new_items.append(base)
                else:
                    new_items.append(p)
            bind.execute(
                sa.text("UPDATE roles SET permissions=:v WHERE id=:i"),
                {"v": json.dumps(new_items), "i": rid},
            )
        # 3. unseed scope_options on PORTFOLIO_* codes
        op.execute(f"""
            UPDATE permission_definitions
            SET scope_options = NULL
            WHERE code IN ({codes_sql})
        """)
