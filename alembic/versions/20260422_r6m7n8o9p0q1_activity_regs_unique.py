"""add partial unique index on activity_registrations (防併發重複報名)

Revision ID: r6m7n8o9p0q1
Revises: q5l6m7n8o9p0
Create Date: 2026-04-22

Why:
  api/activity/public.py `public_register` 以 (student_name, birthday,
  school_year, semester, is_active=TRUE) 做先 SELECT 再 INSERT 的重複報名檢查。
  但兩筆同時送出時，兩邊都查不到既存記錄、都寫入成功，造成同學生同學期
  多筆有效報名（後續同步/統計/帳務都會混亂）。

  partial unique index 讓 DB 層攔下第二筆，router 再捕 IntegrityError 轉 400。

歷史資料清理：
  若生產庫已有違規資料（罕見，多為手動建立或歷史 bug），保留 id 最小的一筆；
  其餘改 is_active=FALSE 軟刪，remark 追加標記以保留稽核線索。

部署前建議 pre-flight：
  SELECT student_name, birthday, school_year, semester, parent_phone, COUNT(*)
    FROM activity_registrations WHERE is_active = TRUE
   GROUP BY 1,2,3,4,5 HAVING COUNT(*) > 1;
  若結果非空，意味著生產已累積重複有效報名，遷移會把較晚的軟刪；事先知悉才
  能與管理員確認處理方式。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "r6m7n8o9p0q1"
down_revision = "q5l6m7n8o9p0"
branch_labels = None
depends_on = None


_TABLE = "activity_registrations"
_INDEX_NAME = "uq_activity_regs_student_term_active"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return

    existing_indexes = {idx["name"] for idx in inspector.get_indexes(_TABLE)}
    if _INDEX_NAME in existing_indexes:
        return

    # 清理歷史重複：保留每組 (name, birthday, year, sem, phone) 中 id 最小的一筆，
    # 其餘改 is_active=FALSE。remark 追加標記供管理員事後檢視。
    # 加 parent_phone 到 partition key，避免誤刪「不同家庭同姓同生日」的合法第二筆。
    op.execute(sa.text("""
            UPDATE activity_registrations a
               SET is_active = FALSE,
                   remark = COALESCE(a.remark, '') || ' [遷移自動軟刪重複報名]'
              FROM (
                  SELECT id,
                         ROW_NUMBER() OVER (
                             PARTITION BY student_name, birthday,
                                          school_year, semester, parent_phone
                             ORDER BY id ASC
                         ) AS rn
                    FROM activity_registrations
                   WHERE is_active = TRUE
              ) dup
             WHERE a.id = dup.id
               AND dup.rn > 1
            """))

    op.create_index(
        _INDEX_NAME,
        _TABLE,
        ["student_name", "birthday", "school_year", "semester", "parent_phone"],
        unique=True,
        postgresql_where=sa.text("is_active = TRUE"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    existing_indexes = {idx["name"] for idx in inspector.get_indexes(_TABLE)}
    if _INDEX_NAME in existing_indexes:
        op.drop_index(_INDEX_NAME, table_name=_TABLE)
