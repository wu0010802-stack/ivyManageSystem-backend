"""[P1] 補 GRANT UPDATE ON activity_courses → ivy_parent_role（FOR UPDATE 需 UPDATE 權）

登入家長端的 register_courses（api/parent_portal/activity.py）與
confirm_waitlist_promotion / decline_waitlist_promotion（services/activity_service.py）
為防並發超賣，對 activity_courses 列下 `SELECT ... FOR UPDATE` 序列化「讀容量→判
enrolled/waitlist→寫入」。這些路徑走 ivy_parent_role（parent RLS engine，get_parent_db）。

PostgreSQL 的 row-locking 子句（FOR UPDATE / FOR NO KEY UPDATE / FOR SHARE / FOR KEY
SHARE）**要求該表的 UPDATE 權限**，不是只要 SELECT。但 parlsr007（phase1f）對 catalog
表只 `GRANT SELECT ON activity_courses TO ivy_parent_role`（catalog 無 RLS、只給讀）。
故任一登入家長打「報名課程 / 確認候補轉正 / 放棄候補」時，FOR UPDATE 對 activity_courses
觸發 `permission denied for table activity_courses`（InsufficientPrivilege，屬
ProgrammingError）→ 裸 500，登入家長三個核心端點全斷（公開未登入端走 get_session admin
引擎不受影響，故症狀為「未登入能報、登入反而壞」）。

此漏洞先前未被測試攔到：parent activity 單元測試跑 SQLite（FOR UPDATE 為 no-op、無權限
系統 → 恆綠）；唯一的 real-PG parent grant 測試（tests/spike_rls/test_rls_phase1f.py）
僅對 catalog 跑 plain SELECT，從未下 FOR UPDATE。

修法：補 `GRANT UPDATE ON activity_courses TO ivy_parent_role`。FOR UPDATE 僅需 table-level
UPDATE 權即可取得行鎖；catalog 表（activity_courses）未啟用 RLS，且家長端程式碼路徑中對
activity_courses 沒有任何 UPDATE 語句（只有 FOR UPDATE 鎖），故補此 grant 不擴大實際寫入
面、不增風險。對照同 migration（parlsr007）已對 activity_registrations / registration_courses
基於相同的 FOR UPDATE 需求給了 UPDATE 權，本 migration 補上被遺漏的 catalog 表。

註：register/confirm/decline 的另一個鎖目標 registration_courses / activity_registrations
已於 parlsr007 取得 UPDATE 權；activity_supplies 雖同為 SELECT-only catalog，但家長端路徑
未對其下 FOR UPDATE，故本 migration 不動 activity_supplies（最小權限）。

downgrade：REVOKE 回 SELECT-only 狀態。

Revision ID: parcuplk01
Revises: pcntsp01
Create Date: 2026-06-25
"""

from __future__ import annotations

from alembic import op

revision = "parcuplk01"
down_revision = "pcntsp01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite / 其他 dialect 無 ivy_parent_role 角色（測試走 UDF shim），GRANT 不適用。
        return
    op.execute("GRANT UPDATE ON activity_courses TO ivy_parent_role;")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("REVOKE UPDATE ON activity_courses FROM ivy_parent_role;")
