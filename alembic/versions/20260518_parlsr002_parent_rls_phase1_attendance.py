"""parent_rls_phase1_attendance: GRANT + ENABLE RLS + POLICY for student_attendances + guardians

Revision ID: parlsr002
Revises: parlsr001
Create Date: 2026-05-18

Phase 1 pilot — **`attendance.py` 路由切到 parent_engine 的 DB 配套**。

範圍刻意縮到 read-only：兩張表上 RLS、兩個 router 端點切換，沒碰 leaves.py、
medication、fees 等任何寫路徑。原始設計 §4 Phase 1 包 leaves.py，spike 過程
盤點發現 leaves.py 還會觸 Attachment (Class D 多型 phase 4) / Holiday +
WorkdayOverride (neutral 表須另設 GRANT) / student_attendances DELETE
(revert_attendance_for_leave 用硬刪)，與 phase 1 pilot 應有的 minimal-scope
精神牴觸，所以 leaves.py 延到 phase 1b 與後續 GRANT 補齊一起處理。

# What this migration does
1. GRANT SELECT ON student_attendances TO ivy_parent_role
2. GRANT SELECT ON guardians TO ivy_parent_role
   （RLS policy 的 sub-query 與應用層 _assert_student_owned 都需要）
3. ALTER TABLE student_attendances ENABLE + FORCE ROW LEVEL SECURITY
4. ALTER TABLE guardians ENABLE + FORCE ROW LEVEL SECURITY
5. CREATE POLICY parent_isolate_attendance ON student_attendances (Class A — JOIN guardians)
6. CREATE POLICY parent_self_guardian ON guardians (Class B — user_id direct)

FORCE 模式很重要：即便 table owner（ivy_owner_role / migration runner）也受 policy
約束。為了讓 owner 仍能跑 backfill / 維護腳本，那些操作必須在 admin_login（BYPASSRLS）
身分下跑，或在 session 內 SET LOCAL ROLE 切回 admin role。

# What this migration deliberately doesn't do
- 不 GRANT INSERT/UPDATE/DELETE 給 ivy_parent_role (read-only pilot)
- 不對 leaves / medication / fees / message 等其他表動 RLS
- 不切換任何 router (那一步在 api/parent_portal/attendance.py 的 Python 改動裡)
- 不動 ivy_admin_login - BYPASSRLS attribute 仍有效，admin engine 透明繞過

# Downgrade
完全對稱：DROP POLICY → DISABLE RLS → REVOKE。downgrade 後 admin engine 對兩表行為
與 phase 0 完全一致。
"""

from __future__ import annotations

from alembic import op

revision = "parlsr002"
down_revision = "parlsr001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. GRANT SELECT ────────────────────────────────────────────────────
    op.execute("""
        GRANT SELECT ON student_attendances TO ivy_parent_role;
        GRANT SELECT ON guardians TO ivy_parent_role;
        """)

    # ── 2. ENABLE + FORCE RLS ─────────────────────────────────────────────
    op.execute("""
        ALTER TABLE student_attendances ENABLE ROW LEVEL SECURITY;
        ALTER TABLE student_attendances FORCE ROW LEVEL SECURITY;
        ALTER TABLE guardians ENABLE ROW LEVEL SECURITY;
        ALTER TABLE guardians FORCE ROW LEVEL SECURITY;
        """)

    # ── 3. Policies ────────────────────────────────────────────────────────
    # Class A (student_attendances): row visible if student belongs to current parent.
    # SubQuery 透過 ix_guardians_user_active partial covering index 高效命中。
    op.execute("""
        CREATE POLICY parent_isolate_attendance ON student_attendances
        FOR SELECT TO ivy_parent_role
        USING (
            student_id IN (
                SELECT g.student_id FROM guardians g
                WHERE g.user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
                  AND g.deleted_at IS NULL
            )
        )
        """)

    # Class B (guardians): parent only sees their own guardian rows.
    # 不用 JOIN，純比較 user_id；NULLIF + ::int 確保 app.current_user_id 未設時
    # 整支 query 被視為 user_id = NULL → 0 row (fail-closed).
    op.execute("""
        CREATE POLICY parent_self_guardian ON guardians
        FOR SELECT TO ivy_parent_role
        USING (
            user_id = NULLIF(current_setting('app.current_user_id', true), '')::int
        )
        """)


def downgrade() -> None:
    # ── 3. Policies ────────────────────────────────────────────────────────
    op.execute("DROP POLICY IF EXISTS parent_self_guardian ON guardians")
    op.execute("DROP POLICY IF EXISTS parent_isolate_attendance ON student_attendances")

    # ── 2. DISABLE RLS ────────────────────────────────────────────────────
    op.execute("""
        ALTER TABLE guardians NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE guardians DISABLE ROW LEVEL SECURITY;
        ALTER TABLE student_attendances NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE student_attendances DISABLE ROW LEVEL SECURITY;
        """)

    # ── 1. REVOKE ──────────────────────────────────────────────────────────
    op.execute("""
        REVOKE SELECT ON guardians FROM ivy_parent_role;
        REVOKE SELECT ON student_attendances FROM ivy_parent_role;
        """)
