"""appraisal + year_end: 註冊 8 個 Permission bit 到既有預設角色（M1 重構版）

新增 / 確認 8 個 Permission bit 並更新明確指派過權限的使用者：
- 1<<55 APPRAISAL_READ
- 1<<56 APPRAISAL_EVENT_WRITE
- 1<<57 APPRAISAL_REVIEW
- 1<<58 APPRAISAL_ACCOUNTING
- 1<<59 APPRAISAL_FINALIZE
- 1<<52 YEAR_END_READ        (新增於 M1)
- 1<<60 YEAR_END_WRITE       (新增於 M1)
- 1<<61 YEAR_END_FINALIZE    (新增於 M1)

ROLE_ADDONS（與 utils/permissions.py ROLE_TEMPLATES 對齊）：
- admin: -1 全權限，已含新 bit，跳過
- supervisor: APPRAISAL READ/EVENT_WRITE/REVIEW/FINALIZE + YEAR_END READ/WRITE/FINALIZE
- hr: APPRAISAL READ/EVENT_WRITE/ACCOUNTING + YEAR_END READ/WRITE
- teacher: APPRAISAL READ/EVENT_WRITE（年終結算不開放）
- parent: 0，跳過

⚠ 位元 >= 32：前端 bitwise 必須使用 BigInt

Revision ID: a9p0p1r2i3s4
Revises: a3p4p5r6i7s8
Create Date: 2026-05-11 (rewritten 2026-05-15 for M1)
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
YEAR_END_READ = 1 << 52
YEAR_END_WRITE = 1 << 60
YEAR_END_FINALIZE = 1 << 61

ROLE_ADDONS = {
    "hr": (
        APPRAISAL_READ
        | APPRAISAL_EVENT_WRITE
        | APPRAISAL_ACCOUNTING
        | YEAR_END_READ
        | YEAR_END_WRITE
    ),
    "supervisor": (
        APPRAISAL_READ
        | APPRAISAL_EVENT_WRITE
        | APPRAISAL_REVIEW
        | APPRAISAL_FINALIZE
        | YEAR_END_READ
        | YEAR_END_WRITE
        | YEAR_END_FINALIZE
    ),
    "teacher": APPRAISAL_READ | APPRAISAL_EVENT_WRITE,
}

ALL_NEW_BITS = (
    APPRAISAL_READ
    | APPRAISAL_EVENT_WRITE
    | APPRAISAL_REVIEW
    | APPRAISAL_ACCOUNTING
    | APPRAISAL_FINALIZE
    | YEAR_END_READ
    | YEAR_END_WRITE
    | YEAR_END_FINALIZE
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" not in inspector.get_table_names():
        return
    for role_name, mask in ROLE_ADDONS.items():
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
    mask_clear = ~ALL_NEW_BITS
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
