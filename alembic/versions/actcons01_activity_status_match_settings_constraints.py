"""才藝 status/match_status 值域 CHECK + settings 單列約束

Revision ID: actcons01
Revises: aprsnap01
Create Date: 2026-06-23

Why（2026-06-23 優化盤點 資料模型兜底）:

三個值域/單列約束原本只活在 Python 層、DB 無兜底：

- ck_registration_courses_status：status ∈ RegistrationCourseStatus enum 三值。
  裸 String 無約束，非法值會讓佔容量判定 status.in_(OCCUPYING_STATUSES) 默默
  漏算 → 超賣。寫入路徑窮舉僅 enrolled/waitlist/promoted_pending（dev DB 0 違規）。
- ck_activity_registrations_match_status：match_status ∈ MatchStatus enum 六值
  （unmatched/matched/pending/rejected/manual/forced）。NULL 由 IN 放行（歷史列）。
  寫入路徑窮舉僅此六值 + default unmatched（dev DB 0 違規）。
- uq_activity_registration_settings_singleton：settings 表 docstring 自承「只有
  一列」但 schema 無強制；.first() 取列若誤 insert 第二列會非決定性讀到其一。
  expression unique index ((true)) 讓 DB 保證至多一列（不依賴 id=1、不需改寫入端）。
  dev DB 現恰 1 列。

值域全部抄自 services/activity_status.py enum + 程式碼寫入站點窮舉
（feedback_check_constraint_open_domain：列舉式 CHECK 前已證寫入路徑值域封閉）。
payment_method 暫不加 CHECK：欄位註解「保留供未來擴充」、可為任意值/NULL，值域非
封閉枚舉。

models/activity.py 已同步：兩個 CheckConstraint 進 __table_args__（SQLite 測試 DB
由 metadata 建表，CHECK 兩邊一致）。singleton 為 PG-only expression index，不放
model（避免 SQLite metadata 對常數 index 的相容問題；測試不需此 backstop）。

SQLite：無法 ALTER 加 CHECK / expression index，本 migration 僅在 PostgreSQL 執行。
downgrade：drop index + 2 CHECK。
"""

import logging

from alembic import op

logger = logging.getLogger(__name__)

revision = "actcons01"
down_revision = "aprsnap01"
branch_labels = None
depends_on = None


# (constraint 名, 表名, CHECK 條件 SQL)
_CHECKS = [
    (
        "ck_registration_courses_status",
        "registration_courses",
        "status IN ('enrolled','waitlist','promoted_pending')",
    ),
    (
        "ck_activity_registrations_match_status",
        "activity_registrations",
        "match_status IN "
        "('unmatched','matched','pending','rejected','manual','forced')",
    ),
]

_SETTINGS_SINGLETON_INDEX = "uq_activity_registration_settings_singleton"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        logger.info("非 PostgreSQL（%s），跳過才藝值域/單列約束", bind.dialect.name)
        return

    for name, table, condition in _CHECKS:
        op.create_check_constraint(name, table, condition)
        logger.info("已加 CHECK %s ON %s", name, table)

    # expression unique index：每列 key 皆為 true → 至多一列。
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_SETTINGS_SINGLETON_INDEX} "
        "ON activity_registration_settings ((true))"
    )
    logger.info("已加 singleton 約束 %s", _SETTINGS_SINGLETON_INDEX)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(f"DROP INDEX IF EXISTS {_SETTINGS_SINGLETON_INDEX}")
    for name, table, _condition in _CHECKS:
        op.drop_constraint(name, table, type_="check")
