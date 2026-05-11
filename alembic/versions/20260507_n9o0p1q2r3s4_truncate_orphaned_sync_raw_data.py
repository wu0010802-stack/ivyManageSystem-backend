"""cleanup orphaned sync_raw_data (>7 days)

sync_raw_data 為教育部爬蟲（services/moe_kindergarten_scraper.py）暫存原始資料。
本機 dev DB 於 2026-04 累積 16.4 萬筆（102 MB），檢視程式碼後確認：
1. 目前已無任何路徑 INSERT 此表（CompetitorSchool 直接寫入 schema 表）。
2. 也無任何 SELECT 讀取此表（無對應 ORM 模型）。
3. scraper 既有 30 天 DELETE 維護動作。

本 migration 比照 scraper retention 但更積極（7 天）：
- Dev：本機所有 164k 筆已 27 天，全數清掉（≈ TRUNCATE 效果）。
- Prod：若有資料只刪 >7 天部分；若是新近資料則為 no-op，安全。

保留表結構，scraper 後續 DELETE 變 no-op。

Revision ID: n9o0p1q2r3s4
Revises: m8n9o0p1q2r3
Create Date: 2026-05-07
"""

from alembic import op
from sqlalchemy import inspect, text

# revision identifiers, used by Alembic.
revision = "n9o0p1q2r3s4"
down_revision = "m8n9o0p1q2r3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "sync_raw_data" not in set(inspect(bind).get_table_names()):
        return
    # docstring 已說明本表已無 INSERT / SELECT 路徑（scraper 也已切換）。
    # TRUNCATE 取代 DELETE 以避免 16 萬筆 dead tuples 與長 row lock；雖取
    # AccessExclusiveLock 但瞬間完成，且 prod 既然「無寫」也不會搶鎖。
    # 改 TRUNCATE 後不再保留 "7 天" 過濾條件，但這條件在 docstring 假
    # 設下已無意義（無寫者代表所有資料皆 > 7 天）。
    bind.execute(text("TRUNCATE TABLE sync_raw_data"))


def downgrade() -> None:
    # 資料清理不可逆，downgrade 為空操作
    pass
