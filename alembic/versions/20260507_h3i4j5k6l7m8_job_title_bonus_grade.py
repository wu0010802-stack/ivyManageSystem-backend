"""job_titles 加 bonus_grade 欄位（階段 2-D）

把原本 hardcode 在 services/salary/constants.py:POSITION_GRADE_MAP
（"幼兒園教師":"A", "教保員":"B", "助理教保員":"C"）搬到 DB。

Why:
- 業主可能會擴增職稱（例如新增「資深教保員」要算 A 級），原本必須改 .py 檔
  + 重新部署。改成 DB 後，行政在 UI 設一次即可。
- 配合階段 2-A/B/C 落地策略：所有跟錢相關的常數能進 DB 就進 DB。

Revision ID: h3i4j5k6l7m8
Revises: g2h3i4j5k6l7
Create Date: 2026-05-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "h3i4j5k6l7m8"
down_revision = "g2h3i4j5k6l7"
branch_labels = None
depends_on = None


# 預設 mapping（與舊 POSITION_GRADE_MAP 一致）
# 其他職稱（園長/司機/廚工/職員）等級為 NULL，因為走主管 / office_staff 路徑
# 不經過 grade-based 節慶獎金計算。
_DEFAULT_GRADES = {
    "幼兒園教師": "A",
    "教保員": "B",
    "助理教保員": "C",
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "job_titles" not in inspector.get_table_names():
        return

    cols = {c["name"] for c in inspector.get_columns("job_titles")}
    if "bonus_grade" not in cols:
        op.add_column(
            "job_titles",
            sa.Column(
                "bonus_grade",
                sa.CHAR(1),
                nullable=True,
                comment="節慶獎金等級（A/B/C）；NULL=非帶班職稱不適用",
            ),
        )

    # 回填預設等級（只覆蓋現有為 NULL 的列，避免覆蓋管理員手動設定）
    for name, grade in _DEFAULT_GRADES.items():
        op.execute(
            sa.text(
                "UPDATE job_titles SET bonus_grade = :g "
                "WHERE name = :n AND bonus_grade IS NULL"
            ).bindparams(g=grade, n=name)
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "job_titles" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("job_titles")}
    if "bonus_grade" in cols:
        op.drop_column("job_titles", "bonus_grade")
