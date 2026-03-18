"""add DISMISSAL_CALLS_READ/WRITE permissions to existing teacher accounts

接送通知 portal 端點補齊 require_permission 守衛所需的權限位：
- DISMISSAL_CALLS_READ  = 1 << 29
- DISMISSAL_CALLS_WRITE = 1 << 30

upgrade：對所有 role='teacher' 的 User 記錄 OR 進新增位元，
          確保部署後現有教師帳號不失去 portal 存取權。
downgrade：AND NOT 移除相同位元。

Revision ID: j6k7l8m9n0o1
Revises: i5j6k7l8m9n0
Create Date: 2026-03-17 00:30:00.000000
"""

from alembic import op

revision = "j6k7l8m9n0o1"
down_revision = "i5j6k7l8m9n0"
branch_labels = None
depends_on = None

_DISMISSAL_CALLS_READ  = 1 << 29   # 536870912
_DISMISSAL_CALLS_WRITE = 1 << 30   # 1073741824
_NEW_BITS = _DISMISSAL_CALLS_READ | _DISMISSAL_CALLS_WRITE


def upgrade() -> None:
    # 對所有 teacher 帳號 OR 進新增位元（permissions = -1 的 admin 帳號不受影響，
    # 因為 -1 在 PostgreSQL INTEGER/BIGINT 中代表全部位元已設）
    op.execute(
        f"UPDATE users SET permissions = permissions | {_NEW_BITS} "
        f"WHERE role = 'teacher' AND permissions != -1"
    )


def downgrade() -> None:
    op.execute(
        f"UPDATE users SET permissions = permissions & ~{_NEW_BITS} "
        f"WHERE role = 'teacher' AND permissions != -1"
    )
