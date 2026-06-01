"""student enrollment numbering: 永久編號欄位 + 約束 + backfill

Revision ID: studnum01
Revises: mergeheads08
Create Date: 2026-06-01

說明：
- 新增 students.enrollment_school_year（Integer, nullable）
- 新增 students.enrollment_seq（Integer, nullable）
- 移除舊 unique 約束 students_student_id_key（student_id 改為僅 index）
- 建立 ix_students_student_id index（若 model 層已宣告則 checkfirst）
- 建立 uq_students_enrollment_year_seq 複合唯一鍵
- 對既有學生執行 backfill_enrollment_numbers（冪等）

unique 約束名 students_student_id_key 為 Postgres 對 Column(unique=True)
的預設命名，已以 \\d students 及 pg_constraint 查詢確認（2026-06-01 dev DB）。

downgrade 不可逆部分：student_id 顯示快取已重算為新格式
（{學年}-{年級字}-{seq:02d}），downgrade 不會還原原始 {學年}-{班代}-{NN} 格式。

本 migration 含 ORM data migration（backfill），**僅支援 online 套用**
（`alembic upgrade head` 直連 DB）；不支援 offline `alembic upgrade --sql`
（backfill 在 MockConnection 下無法執行）。部署 SOP 勿依賴 --sql 預演。
"""

from alembic import op
import sqlalchemy as sa

revision = "studnum01"
down_revision = "mergeheads08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "students",
        sa.Column(
            "enrollment_school_year",
            sa.Integer(),
            nullable=True,
            comment="發號學年（民國）；身分認定鍵之一，永久不變",
        ),
    )
    op.add_column(
        "students",
        sa.Column(
            "enrollment_seq",
            sa.Integer(),
            nullable=True,
            comment="永久流水號；入學配發一次、終身不變",
        ),
    )
    op.drop_constraint("students_student_id_key", "students", type_="unique")
    op.create_index("ix_students_student_id", "students", ["student_id"])
    op.create_unique_constraint(
        "uq_students_enrollment_year_seq",
        "students",
        ["enrollment_school_year", "enrollment_seq"],
    )

    from sqlalchemy.orm import Session
    from services.student_numbering import backfill_enrollment_numbers

    bind = op.get_bind()
    sess = Session(bind=bind)
    backfill_enrollment_numbers(sess)
    sess.flush()


def downgrade() -> None:
    op.drop_constraint(
        "uq_students_enrollment_year_seq", "students", type_="unique"
    )
    op.drop_index("ix_students_student_id", table_name="students")

    # 新 student_id 顯示格式刻意允許罕見重複（跨屆同 seq 同年同年級）。
    # 重建舊 unique 前先偵測碰撞，有則 fail-fast 要求人工處理，
    # 避免 ADD CONSTRAINT 拋出隱晦的 IntegrityError。
    bind = op.get_bind()
    dupes = bind.execute(
        sa.text(
            "SELECT student_id, COUNT(*) AS c FROM students "
            "GROUP BY student_id HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if dupes:
        raise RuntimeError(
            f"downgrade 受阻：student_id 顯示快取有 {len(dupes)} 組重複值，"
            "無法重建 students_student_id_key unique 約束。"
            "請先人工去除重複的 student_id 再執行 downgrade。"
        )
    op.create_unique_constraint(
        "students_student_id_key", "students", ["student_id"]
    )
    op.drop_column("students", "enrollment_seq")
    op.drop_column("students", "enrollment_school_year")
