"""年終獎金常數。"""

from __future__ import annotations

from decimal import Decimal

# 節慶獎金月份（年度節慶獎金總額會 4 個月加總）
FESTIVAL_MONTHS: tuple[int, ...] = (2, 6, 9, 12)

# 滿勤年資（step5 比例底數）
FULL_YEAR_MONTHS: Decimal = Decimal("12")

# 班級績效平均的兩個欄位（廚工/職員缺此兩項時跳過）
CLASS_PERFORMANCE_FIELDS: tuple[str, ...] = (
    "returning_rate_first",
    "returning_rate_second",
    "achievement_rate_first",
    "achievement_rate_second",
)
