"""seed 雜項收款 permission_definitions + roles 補授

mscrcpt01（2026-06-29）建立 misc_receipts 資料表；Task 3 已把
MISC_RECEIPT_READ / MISC_RECEIPT_WRITE 加進 in-code Permission enum、
PERMISSION_LABELS、PERMISSION_GROUPS（園務行政 group）、以及
ROLE_TEMPLATES（hr / supervisor / accountant）。

但 rolesdb01（2026-05-25）早已跑過，無法回溯 seed DB，因此本 migration
補兩件事：
  1. permission_definitions：插入兩筆新碼（idempotent，已存在則略過）。
  2. roles：呼叫 sync_core_role_permissions 將 ROLE_TEMPLATES 中已新增的
     misc_receipt 碼補入 DB roles array（僅 additive，不刪既有）。

Revision ID: mscrcptp01
Revises: mscrcpt01
Create Date: 2026-06-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "mscrcptp01"
down_revision = "mscrcpt01"
branch_labels = None
depends_on = None

_DEFS = [
    {
        "code": "MISC_RECEIPT_READ",
        "label": "雜項收款 (檢視)",
        "group_name": "園務行政",
    },
    {
        "code": "MISC_RECEIPT_WRITE",
        "label": "雜項收款 (編輯/簽收)",
        "group_name": "園務行政",
    },
]


def upgrade() -> None:
    from utils.permission_backfill import sync_core_role_permissions

    conn = op.get_bind()

    # 取得已存在的碼（idempotent）
    existing = {
        r[0] for r in conn.execute(sa.text("SELECT code FROM permission_definitions"))
    }
    rows = [d for d in _DEFS if d["code"] not in existing]
    if rows:
        conn.execute(
            sa.text(
                "INSERT INTO permission_definitions (code, label, group_name, is_core) "
                "VALUES (:code, :label, :group_name, true)"
            ),
            rows,
        )

    # 讀 in-code ROLE_TEMPLATES（Task 3 已加 hr/supervisor/accountant），
    # 把缺漏的碼補進 DB roles array
    sync_core_role_permissions(conn)


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "DELETE FROM permission_definitions "
            "WHERE code IN ('MISC_RECEIPT_READ','MISC_RECEIPT_WRITE')"
        )
    )
    # roles array 內的碼為 additive（比照 permbf01 慣例），downgrade 不移除以避免
    # 誤刪 admin 已手動授出的 grant。
