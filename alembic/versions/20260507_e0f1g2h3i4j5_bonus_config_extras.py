"""bonus_configs 補園規常數欄位 + 修 art_teacher 載入 bug

加入下列欄位讓行政可在 UI 調整，無需改程式碼：
- `meeting_default_hours`：每場園務會議計幾小時加班費（業主實務 2 hr）
- `meeting_absence_penalty`：缺席園務會議扣節慶獎金金額（預設 100 元）
- `art_teacher_festival`：美語/才藝教師節慶獎金基數（A/B/C 同值，預設 2000）

Why: 原本這 3 個值都 hardcode 在 services/salary/constants.py。
- DEFAULT_MEETING_HOURS=1 與業主實務（2 hr）不一致 → 系統建會議用 default 時會議費永遠少一半
- art_teacher 沒進 BonusConfig，且 _load_config_from_db_locked 重建 _bonus_base 時會把
  art_teacher key 連同 hardcode 的 2000 一起抹掉 → production 美語老師節慶永遠 0（既有 bug）

Revision ID: e0f1g2h3i4j5
Revises: d9e0f1g2h3i4
Create Date: 2026-05-07
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "e0f1g2h3i4j5"
down_revision = "d9e0f1g2h3i4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "bonus_configs" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("bonus_configs")}

    if "meeting_default_hours" not in cols:
        op.add_column(
            "bonus_configs",
            sa.Column(
                "meeting_default_hours",
                sa.Float(),
                nullable=True,
                comment="每場園務會議計幾小時加班費（NULL=沿用程式預設 2.0）",
            ),
        )
    if "meeting_absence_penalty" not in cols:
        op.add_column(
            "bonus_configs",
            sa.Column(
                "meeting_absence_penalty",
                sa.Integer(),
                nullable=True,
                comment="缺席園務會議扣節慶獎金金額（NULL=沿用程式預設 100）",
            ),
        )
    if "art_teacher_festival" not in cols:
        op.add_column(
            "bonus_configs",
            sa.Column(
                "art_teacher_festival",
                sa.Float(),
                nullable=True,
                comment="美語/才藝教師節慶獎金基數（A/B/C 同值，NULL=沿用程式預設 2000）",
            ),
        )

    # 回填現有 active row 為「業主實務」值，避免新欄位上線後既有計算行為突然改變
    op.execute("""
        UPDATE bonus_configs
        SET meeting_default_hours = COALESCE(meeting_default_hours, 2.0),
            meeting_absence_penalty = COALESCE(meeting_absence_penalty, 100),
            art_teacher_festival = COALESCE(art_teacher_festival, 2000)
        """)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "bonus_configs" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("bonus_configs")}
    for col in (
        "meeting_default_hours",
        "meeting_absence_penalty",
        "art_teacher_festival",
    ):
        if col in cols:
            op.drop_column("bonus_configs", col)
