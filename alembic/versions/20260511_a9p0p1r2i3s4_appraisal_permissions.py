"""appraisal: 註冊 5 個 Permission bit 到既有預設角色

新增 5 個 Permission bit（1<<55 ~ 1<<59）並更新現有使用者的明確權限遮罩：
- admin：-1 全權限，已含新 bit，跳過
- supervisor：READ + EVENT_WRITE + REVIEW + FINALIZE
- hr：READ + EVENT_WRITE + ACCOUNTING
- teacher：READ + EVENT_WRITE
- parent：0，無任何 bit，跳過

⚠ 本系統無獨立 roles 表；角色預設已於 utils/permissions.py ROLE_TEMPLATES 定義。
  此 migration 只針對 users.permissions 欄位非 NULL 且非 -1（全權限）的使用者補位元，
  確保明確指派過權限的帳號同步取得考核模組存取權。

⚠ 位元 >= 32：前端 bitwise 必須使用 BigInt（utils/permissions.py 已有警告註解）

Revision ID: a9p0p1r2i3s4
Revises: a3p4p5r6i7s8
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op

revision = "a9p0p1r2i3s4"
down_revision = "a3p4p5r6i7s8"
branch_labels = None
depends_on = None

# 與 utils/permissions.py 對齊
APPRAISAL_READ = 1 << 55
APPRAISAL_EVENT_WRITE = 1 << 56
APPRAISAL_REVIEW = 1 << 57
APPRAISAL_ACCOUNTING = 1 << 58
APPRAISAL_FINALIZE = 1 << 59

# 本系統實際存在的角色（users.role）對應要加的 mask
# 對應 utils/permissions.py ROLE_TEMPLATES
# - admin: -1 全權限，已含新 bit，跳過
# - parent: 0，無任何 bit，跳過
ROLE_ADDONS = {
    "hr": APPRAISAL_READ | APPRAISAL_EVENT_WRITE | APPRAISAL_ACCOUNTING,
    "supervisor": (
        APPRAISAL_READ | APPRAISAL_EVENT_WRITE | APPRAISAL_REVIEW | APPRAISAL_FINALIZE
    ),
    "teacher": APPRAISAL_READ | APPRAISAL_EVENT_WRITE,
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in inspector.get_table_names():
        return
    for role_name, mask in ROLE_ADDONS.items():
        # 只更新明確指派過權限（非 NULL 且非 -1）的使用者
        # NULL → 使用角色預設（ROLE_TEMPLATES 已在程式碼層更新，無需 migration 介入）
        # -1   → 全部權限，本已含新 bit，無需更新
        bind.execute(
            sa.text(
                "UPDATE users "
                "SET permissions = COALESCE(permissions, 0) | :mask "
                "WHERE role = :role "
                "  AND permissions IS NOT NULL "
                "  AND permissions != -1"
            ),
            {"mask": mask, "role": role_name},
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in inspector.get_table_names():
        return
    # 注意：不要 & 0xFFFFFFFFFFFFFFFF，那會把結果轉為無號 64-bit 整數，
    # 超過 PostgreSQL signed BIGINT 上限，psycopg2 會 raise NumericValueOutOfRange。
    # Python 直接讓結果為負整數即可，PostgreSQL 對 signed BIGINT 的 bitwise AND 正確處理。
    mask_clear = ~((1 << 55) | (1 << 56) | (1 << 57) | (1 << 58) | (1 << 59))
    # 對稱化：upgrade 只動 hr/supervisor/teacher 三 role，downgrade 也限縮到相同 role，
    # 避免清掉手動指派此 5 bit 的特殊帳號（雖然在預設角色下實務上是 no-op，但行為對稱）。
    bind.execute(
        sa.text(
            "UPDATE users "
            "SET permissions = permissions & :mask "
            "WHERE permissions IS NOT NULL "
            "  AND permissions != -1 "
            "  AND role IN ('hr', 'supervisor', 'teacher')"
        ),
        {"mask": mask_clear},
    )
