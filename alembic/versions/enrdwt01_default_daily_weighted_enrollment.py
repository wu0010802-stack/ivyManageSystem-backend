"""現有 BonusConfig 在籍人數模式切為 daily_weighted（決策1，業主選按日加權）

業主 2026-06-13 決定：節慶/超額獎金的班級在籍人數改用「按日加權平均」
（學生月中進出按天比例計），取代月底單日快照。此為一次性資料遷移，把
**現有** BonusConfig 列切過去；model/server default 維持 month_end（零漂移
保險，全新環境/測試不變），新環境上線後於設定頁選按日加權即可。

注意：與業主既有 Excel「月底人數」慣例會產生差異（月中無進出的班級不受
影響——整月在籍按日加權＝1.0）。

Refs: docs/superpowers/specs/2026-06-13-enrollment-count-correctness-design.md
Revision ID: enrdwt01
Revises: enrmode01
"""

from alembic import op

revision = "enrdwt01"
down_revision = "enrmode01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE bonus_configs SET enrollment_count_mode = 'daily_weighted' "
        "WHERE enrollment_count_mode = 'month_end'"
    )


def downgrade() -> None:
    # 回退：把本遷移切過的列改回 month_end。注意若上線後曾於 UI 再調整，
    # downgrade 會一併回到 month_end（資料遷移固有限制，downgrade 罕用）。
    op.execute(
        "UPDATE bonus_configs SET enrollment_count_mode = 'month_end' "
        "WHERE enrollment_count_mode = 'daily_weighted'"
    )
