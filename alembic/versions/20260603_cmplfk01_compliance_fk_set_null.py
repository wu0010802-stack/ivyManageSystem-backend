"""compliance FK CASCADE → SET NULL：稽核/同意/DSR 紀錄不隨主體硬刪連坐消失（RA-MED-9）

三個「主體」FK 原為 ON DELETE CASCADE，硬刪 user/student 會連坐抹除合規紀錄：
- parent_consent_log.user_id    → users.id     （同意證明）
- dsr_requests.user_id          → users.id     （DSR 申請史）
- medical_access_log.student_id → students.id  （個資法 §6 醫療取用稽核）

改為 ON DELETE SET NULL（並把欄位改 nullable），與同表既有 sibling FK 一致
（dsr_requests reviewer、medical_access_log accessor user_id 皆已 SET NULL，後者欄位
註解明寫「離職員工 deleted 後可變 NULL 保留稽核軌跡」）：主體被刪時合規紀錄存活，
僅 detach 主體連結。

不採 RESTRICT：避免破壞既有硬刪流程（api/auth.py session.delete(user)、
services/recruitment_funnel.py session.delete(student)）——RESTRICT 會讓那兩個 delete
在有合規紀錄時拋 IntegrityError。

models/base.py 無 naming_convention → FK 為 PostgreSQL 預設名 <table>_<col>_fkey。
欄位上原本的 index=True 由獨立 ix_<table>_<col> 索引承載，drop/recreate FK 不影響它。

Revision ID: cmplfk01
Revises: yeatpunch01
"""

from alembic import op
import sqlalchemy as sa

revision = "cmplfk01"
down_revision = "yeatpunch01"
branch_labels = None
depends_on = None


# (table, column, ref_table, ref_column, fk_name)
_FKS = [
    ("parent_consent_log", "user_id", "users", "id", "parent_consent_log_user_id_fkey"),
    ("dsr_requests", "user_id", "users", "id", "dsr_requests_user_id_fkey"),
    (
        "medical_access_log",
        "student_id",
        "students",
        "id",
        "medical_access_log_student_id_fkey",
    ),
]


def upgrade():
    for table, col, ref_table, ref_col, fk_name in _FKS:
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.alter_column(table, col, existing_type=sa.Integer(), nullable=True)
        op.create_foreign_key(
            fk_name,
            table,
            ref_table,
            [col],
            [ref_col],
            ondelete="SET NULL",
        )


def downgrade():
    # 還原 ON DELETE CASCADE + NOT NULL。
    # ⚠ 成功 downgrade 會重新開啟 RA-MED-9：硬刪 user/student 又會連坐抹除合規紀錄。
    #   僅緊急時 downgrade，且應儘速 re-upgrade。
    # 注意：若 downgrade 時這些表已存在 NULL 主體列（升級後新刪除造成），NOT NULL 還原
    # 會失敗——屬預期，需先清理或回填孤兒列再 downgrade。
    for table, col, ref_table, ref_col, fk_name in _FKS:
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.alter_column(table, col, existing_type=sa.Integer(), nullable=False)
        op.create_foreign_key(
            fk_name,
            table,
            ref_table,
            [col],
            [ref_col],
            ondelete="CASCADE",
        )
