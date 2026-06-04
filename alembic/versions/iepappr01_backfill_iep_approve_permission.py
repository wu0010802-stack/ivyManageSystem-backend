"""backfill：園長/主任 User 補 STUDENTS_IEP_APPROVE（IEP 批核改走 permission）

Revision ID: iepappr01
Revises: cmplfk01
Create Date: 2026-06-03

IEP approve/close 從 Employee.supervisor_role 字串旁路改走
require_permission(STUDENTS_IEP_APPROVE)。為保留現狀「園長/主任可批核」能力：

- **顯式 permission_names（非 NULL）的主管** → array_append IEP_APPROVE（本 migration）。
- **permission_names=NULL（依角色預設）的主管** → 不動；靠 ROLE_TEMPLATES
  （supervisor/principal 已含 STUDENTS_IEP_APPROVE）。**不可** COALESCE+append，
  否則把「依預設」變成只剩單一 perm、刪掉其餘角色預設權限。
- admin（'*' wildcard）不需。

⚠ 部署後驗證（gap）：若存在 `role NOT IN ('supervisor','principal','admin')`
且 `permission_names IS NULL` 的 supervisor_role 主管，其 role template 不含
IEP_APPROVE → 會失去批核能力，須手動補 permission 或調整角色。查驗 SQL：
  SELECT u.id,u.username,u.role FROM users u JOIN employees e ON u.employee_id=e.id
  WHERE e.supervisor_role IN ('園長','主任') AND u.permission_names IS NULL
    AND u.role NOT IN ('supervisor','principal','admin');
"""

from typing import Sequence, Union

from alembic import op

revision: str = "iepappr01"
down_revision: Union[str, Sequence[str], None] = "cmplfk01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE users
        SET permission_names = array_append(permission_names, 'STUDENTS_IEP_APPROVE')
        WHERE employee_id IN (
            SELECT id FROM employees WHERE supervisor_role IN ('園長', '主任')
        )
          AND permission_names IS NOT NULL
          AND NOT ('STUDENTS_IEP_APPROVE' = ANY(permission_names))
          AND NOT ('*' = ANY(permission_names))
        """)


def downgrade() -> None:
    op.execute("""
        UPDATE users
        SET permission_names = array_remove(permission_names, 'STUDENTS_IEP_APPROVE')
        WHERE employee_id IN (
            SELECT id FROM employees WHERE supervisor_role IN ('園長', '主任')
        )
          AND permission_names IS NOT NULL
        """)
