"""grant lifecycle/guardian/convert bits to existing supervisor users

學生生命週期追蹤 Phase A 新增 4 個權限位元，角色模板 `supervisor` 已納入，
但**既有** supervisor 帳號的 permissions 欄位在建立當下已凍結，若不回補將
看不到新增的按鈕與端點，造成 403。此 migration 一次性 OR 進新位元。

新增位元：
- STUDENTS_LIFECYCLE_WRITE = 1 << 36
- GUARDIANS_READ           = 1 << 37
- GUARDIANS_WRITE          = 1 << 38
- RECRUITMENT_CONVERT      = 1 << 39

upgrade：對所有 role='supervisor' 的 User 記錄 OR 進新增位元。
          admin (permissions=-1) 已具備全部位元不受影響；teacher 不涉及。
downgrade：AND NOT 移除相同位元。

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-04-19
"""

from alembic import op

revision = "f4a5b6c7d8e9"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


_STUDENTS_LIFECYCLE_WRITE = 1 << 36
_GUARDIANS_READ = 1 << 37
_GUARDIANS_WRITE = 1 << 38
_RECRUITMENT_CONVERT = 1 << 39
_NEW_BITS = (
    _STUDENTS_LIFECYCLE_WRITE
    | _GUARDIANS_READ
    | _GUARDIANS_WRITE
    | _RECRUITMENT_CONVERT
)


def upgrade() -> None:
    op.execute(
        f"UPDATE users SET permissions = permissions | {_NEW_BITS} "
        f"WHERE role = 'supervisor' AND permissions != -1"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE users SET permissions = permissions & ~{_NEW_BITS} "
        f"WHERE role = 'supervisor' AND permissions != -1"
    )
