"""[SEC-2026-0624-04] public_count_enrolled 補 pin search_path（CVE-2018-1058 class）

Revision ID: pcntsp01
Revises: actjsonb01
Create Date: 2026-06-24

public_count_enrolled(int) 是唯一 GRANT EXECUTE 給最低信任 ivy_parent_role 的
SECURITY DEFINER 函式（parlsr007 phase1f 建立、actvcnt01 改為 JOIN is_active），
以函式 owner（高權）執行卻未 pin search_path——屬 CVE-2018-1058 class 的潛在提權
primitive：若日後任一 parent route 出現 SQL injection foothold 能 `SET search_path`
+ `CREATE` shadow relation，未 pin 的 SECURITY DEFINER 函式會以 owner context 解析到
攻擊者物件。現行家長端全參數化、無 raw 連線，故無當前可達攻擊鏈，但此加固消除潛在
primitive、零行為變更。

本 migration 以 CREATE OR REPLACE 補 `SET search_path = pg_catalog, public`
（函式 body 與 actvcnt01 完全一致，僅多 proconfig）。pin 後識別子解析固定走
pg_catalog → public，攻擊者無法以自有 schema 搶先解析 registration_courses /
activity_registrations。`REVOKE CREATE ON SCHEMA public FROM PUBLIC` 屬 schema 權限
+ prod 部署需排程，不在本 migration（待業主排程）。

downgrade：還原為 actvcnt01 的定義（JOIN is_active、無 search_path pin）。
"""

from __future__ import annotations

from alembic import op

revision = "pcntsp01"
down_revision = "actjsonb01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite / 其他 dialect 無此 SECURITY DEFINER 函式（測試走 UDF shim）。
        return
    op.execute("""
        CREATE OR REPLACE FUNCTION public_count_enrolled(p_course_id int)
        RETURNS bigint
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT count(*)
            FROM registration_courses rc
            JOIN activity_registrations ar ON ar.id = rc.registration_id
            WHERE rc.course_id = p_course_id
              AND rc.status IN ('enrolled', 'promoted_pending')
              AND ar.is_active = true;
        $$;
        GRANT EXECUTE ON FUNCTION public_count_enrolled(int) TO ivy_parent_role;
        """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("""
        CREATE OR REPLACE FUNCTION public_count_enrolled(p_course_id int)
        RETURNS bigint
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        AS $$
            SELECT count(*)
            FROM registration_courses rc
            JOIN activity_registrations ar ON ar.id = rc.registration_id
            WHERE rc.course_id = p_course_id
              AND rc.status IN ('enrolled', 'promoted_pending')
              AND ar.is_active = true;
        $$;
        GRANT EXECUTE ON FUNCTION public_count_enrolled(int) TO ivy_parent_role;
        """)
