"""補回 rolesdb01 seed 之後新增、DB 未回填的權限定義與角色授權（運作探測 P2-2）。

供 alembic migration `permbf01` 與測試共用。idempotent：只新增缺漏、不刪既有，
可安全重跑。

背景：rolesdb01（2026-05-25）從當時的 PERMISSION_LABELS/ROLE_TEMPLATES seed
permission_definitions/roles；之後新增的 6 個權限碼未回填 → 非 wildcard admin 對
這些功能 403、admin UI 無法授權。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import bindparam
from sqlalchemy.dialects.postgresql import ARRAY

# rolesdb01 之後新增、DB 漏 seed 的權限碼。
# (code, label, group_name)：code/label 對齊 utils.permissions.PERMISSION_LABELS；
# group_name 為 UI 分組（對齊 DB 既有 sibling：學生類→人事教務、其餘 admin→系統）。
_BACKFILL_DEFINITIONS = [
    ("STUDENTS_IEP_APPROVE", "IEP 批核 / 結案", "人事教務"),
    ("DATA_QUALITY_READ", "資料品質報告 — 檢視", "系統"),
    ("DATA_QUALITY_WRITE", "資料品質報告 — 處理", "系統"),
    ("PORTAL_PREVIEW", "預覽教師端", "系統"),
    ("PORTAL_IMPERSONATE", "代為操作教師端", "系統"),
    ("DSR_MANAGE", "個資權利請求管理", "系統"),
]

_BACKFILL_CODES = [c for c, _, _ in _BACKFILL_DEFINITIONS]


def backfill_permission_definitions(conn) -> int:
    """補 permission_definitions 缺漏的 code，回傳新增筆數（idempotent）。"""
    existing = {
        r[0] for r in conn.execute(sa.text("SELECT code FROM permission_definitions"))
    }
    rows = [
        {"code": c, "label": label, "group_name": g}
        for c, label, g in _BACKFILL_DEFINITIONS
        if c not in existing
    ]
    if rows:
        conn.execute(
            sa.text(
                "INSERT INTO permission_definitions (code, label, group_name, is_core) "
                "VALUES (:code, :label, :group_name, true)"
            ),
            rows,
        )
    return len(rows)


def sync_core_role_permissions(conn) -> dict:
    """把 in-code ROLE_TEMPLATES 缺漏的權限併入 DB core roles（只新增、不刪自訂）。

    自動涵蓋 supervisor(+=IEP)/principal(+=DATA_QUALITY_*/PORTAL_PREVIEW)/
    teacher(+=STUDENTS_READ/WRITE:own_class) 等漂移。回傳 {role_code: 新增碼 list}。
    """
    from utils.permissions import ROLE_TEMPLATES

    update_stmt = sa.text(
        "UPDATE roles SET permissions = :perms, updated_at = now() WHERE code = :code"
    ).bindparams(bindparam("perms", type_=ARRAY(sa.Text())))
    added: dict = {}
    for role_code, template_perms in ROLE_TEMPLATES.items():
        row = conn.execute(
            sa.text("SELECT permissions FROM roles WHERE code = :code"),
            {"code": role_code},
        ).fetchone()
        if row is None:
            continue  # DB 無此 role（自訂角色不在模板）→ 跳過
        db_perms = list(row[0] or [])
        missing = [p for p in template_perms if p not in db_perms]
        if missing:
            conn.execute(update_stmt, {"perms": db_perms + missing, "code": role_code})
            added[role_code] = missing
    return added


def run_backfill(conn) -> dict:
    """執行完整回填，回傳摘要 dict。"""
    n_def = backfill_permission_definitions(conn)
    role_added = sync_core_role_permissions(conn)
    return {"definitions_added": n_def, "role_permissions_added": role_added}
