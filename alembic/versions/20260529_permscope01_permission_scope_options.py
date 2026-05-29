"""permission_definitions.scope_options + teacher backfill (Phase 1: STUDENTS_*)

Revision ID: permscope01
Revises: eb0d4cf88f26
Create Date: 2026-05-29
"""

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "permscope01"
down_revision: Union[str, Sequence[str], None] = "eb0d4cf88f26"
branch_labels = None
depends_on = None

SCOPE_AWARE_CODES = (
    "STUDENTS_READ",
    "STUDENTS_WRITE",
    "STUDENTS_LIFECYCLE_WRITE",
)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. 新增 scope_options 欄位
    if dialect == "postgresql":
        op.add_column(
            "permission_definitions",
            sa.Column("scope_options", ARRAY(sa.Text), nullable=True),
        )
    else:
        # SQLite 測試路徑：用 JSON 型別
        op.add_column(
            "permission_definitions",
            sa.Column("scope_options", sa.JSON, nullable=True),
        )

    # 2. seed：3 個 STUDENTS_* codes 標記為 scope-aware
    codes_sql = ", ".join(f"'{c}'" for c in SCOPE_AWARE_CODES)
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

    # 3. teacher role：bare → :own_class（只改 code='teacher' AND is_core=true）
    if dialect == "postgresql":
        op.execute(f"""
            UPDATE roles
            SET permissions = ARRAY(
                SELECT CASE
                    WHEN p IN ({codes_sql}) THEN p || ':own_class'
                    ELSE p
                END
                FROM unnest(permissions) AS p
            )
            WHERE code = 'teacher' AND is_core = true
        """)
    else:
        # SQLite：JSON 欄位，以 Python 轉換
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

    # 4. teacher users：bare → :own_class + bump token_version（跳過 wildcard '*' 持有者）
    if dialect == "postgresql":
        op.execute(f"""
            UPDATE users
            SET permission_names = ARRAY(
                SELECT CASE
                    WHEN p IN ({codes_sql}) THEN p || ':own_class'
                    ELSE p
                END
                FROM unnest(permission_names) AS p
            ),
            token_version = COALESCE(token_version, 0) + 1
            WHERE role = 'teacher'
              AND NOT ('*' = ANY(permission_names))
        """)
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

    if dialect == "postgresql":
        # 還原 teacher users permission_names（剝掉 scope suffix）+ bump token_version
        op.execute("""
            UPDATE users SET permission_names = ARRAY(
                SELECT split_part(p, ':', 1) FROM unnest(permission_names) AS p
            ),
            token_version = COALESCE(token_version, 0) + 1
            WHERE role = 'teacher'
        """)
        # 還原 teacher role permissions
        op.execute("""
            UPDATE roles SET permissions = ARRAY(
                SELECT split_part(p, ':', 1) FROM unnest(permissions) AS p
            )
            WHERE code = 'teacher' AND is_core = true
        """)
    else:
        # SQLite：users 路徑需同時剝 suffix + bump token_version
        rows = bind.execute(
            sa.text(
                "SELECT id, permission_names, token_version FROM users WHERE role='teacher'"
            )
        ).fetchall()
        for uid, names_json, tv in rows:
            items = (
                json.loads(names_json) if isinstance(names_json, str) else names_json
            )
            stripped = [p.split(":")[0] for p in items]
            bind.execute(
                sa.text(
                    "UPDATE users SET permission_names=:v, token_version=:t WHERE id=:i"
                ),
                {"v": json.dumps(stripped), "t": (tv or 0) + 1, "i": uid},
            )
        # roles 路徑：只剝 suffix，不碰 token_version（roles 表無此欄）
        rows = bind.execute(
            sa.text(
                "SELECT id, permissions FROM roles WHERE code='teacher' AND is_core=1"
            )
        ).fetchall()
        for rid, val in rows:
            items = json.loads(val) if isinstance(val, str) else val
            stripped = [p.split(":")[0] for p in items]
            bind.execute(
                sa.text("UPDATE roles SET permissions=:v WHERE id=:i"),
                {"v": json.dumps(stripped), "i": rid},
            )

    op.drop_column("permission_definitions", "scope_options")
