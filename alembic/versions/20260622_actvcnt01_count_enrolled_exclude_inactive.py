"""[Finding 4] public_count_enrolled JOIN activity_registrations 排除 inactive

Revision ID: actvcnt01
Revises: linecred01
Create Date: 2026-06-22

家長端課程目錄的「已報名人數 / is_full」走 SECURITY DEFINER 函式
public_count_enrolled(course_id)（parlsr007 phase1f 建立，bypass RLS 跨家長計數）。

原定義只數 registration_courses 的 status IN ('enrolled','promoted_pending')，
**未 JOIN activity_registrations 篩 is_active**。但後台拒絕報名
（api/activity/registrations_pending.reject_registration）只把
activity_registrations.is_active 設 False，**不改 RegistrationCourse.status**
（仍是 'enrolled'）。因此被拒絕 / 離園的報名其 enrolled RC 會被計入，
家長端誤判額滿、把可入學的孩子錯放候補；公開端 enrolled_count_map
（api/activity/public.py）有 JOIN is_active，兩端口徑因此不一致。

本 migration 以 CREATE OR REPLACE 補上 JOIN + ar.is_active：
  - 對齊公開端 enrolled_count_map 與 _attach_courses 的佔位口徑
  - SQLite 測試端同步改 tests/_parent_rls_test_utils.register_sqlite_parent_rls_udfs

CREATE OR REPLACE FUNCTION 保留既有權限，GRANT 重下為冪等保險。

downgrade：還原為 parlsr007 的原定義（不 JOIN is_active）。
"""

from __future__ import annotations

from alembic import op

revision = "actvcnt01"
down_revision = "linecred01"
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
            SELECT count(*) FROM registration_courses
            WHERE course_id = p_course_id
              AND status IN ('enrolled', 'promoted_pending');
        $$;
        GRANT EXECUTE ON FUNCTION public_count_enrolled(int) TO ivy_parent_role;
        """)
