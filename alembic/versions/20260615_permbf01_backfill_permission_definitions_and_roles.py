"""backfill permission_definitions + roles for codes added after rolesdb01

rolesdb01（2026-05-25）從當時的 PERMISSION_LABELS/ROLE_TEMPLATES seed
permission_definitions/roles；之後新增的 6 個權限碼（STUDENTS_IEP_APPROVE /
DATA_QUALITY_READ / DATA_QUALITY_WRITE / PORTAL_PREVIEW / PORTAL_IMPERSONATE /
DSR_MANAGE）未回填 → 非 wildcard admin 對這些功能 403、admin UI 無法授權
（2026-06-15 運作探測 P2-2）。

回填邏輯在 utils.permission_backfill（與測試共用、idempotent：只新增缺漏、不刪
既有，可安全重跑）：
- permission_definitions：補 in-code 有、DB 缺的 6 碼。
- roles：把 ROLE_TEMPLATES 缺漏的權限併入 core roles（supervisor+=IEP、
  principal+=DATA_QUALITY_*/PORTAL_PREVIEW、teacher+=STUDENTS_READ/WRITE:own_class）。

Revision ID: permbf01
Revises: enrdwt01
Create Date: 2026-06-15
"""

from typing import Sequence, Union

from alembic import op

revision: str = "permbf01"
down_revision: Union[str, None] = "enrdwt01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from utils.permission_backfill import run_backfill

    run_backfill(op.get_bind())


def downgrade() -> None:
    # 本 migration 為 additive 回填（補齊 in-code 早已存在、DB 漏 seed 的權限定義/
    # 角色授權）。downgrade 刻意 no-op：① 刪 permission_definitions 會孤立 admin 後續
    # 手動授出的 grant；② 刪 role permissions 會誤刪可能已沿用的授權。回滾後這些列與
    # in-code 定義一致、無害。如需強制清除請另寫一次性 data migration。
    pass
