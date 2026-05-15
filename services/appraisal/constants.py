"""半年考核計算用常數。

切點 / 獎金百分比 / 角色推薦關鍵字。商業邏輯依 `114上考核表` 反推與業主訪談。
"""

from __future__ import annotations

from decimal import Decimal

from models.appraisal import Grade

# 總分上下限（clamp 用）
MIN_TOTAL_SCORE: Decimal = Decimal("0")
MAX_TOTAL_SCORE: Decimal = Decimal("110")

# 等第切點（>=）
GRADE_CUT_OUTSTANDING: Decimal = Decimal("90")
GRADE_CUT_GOOD: Decimal = Decimal("80")
GRADE_CUT_PASS: Decimal = Decimal("70")
GRADE_CUT_WARN: Decimal = Decimal("60")

# 等第對應的獎金百分比（Excel 反推：優=100% / 甲=80% / 乙=60% / 丙、丁=0%）
GRADE_BONUS_PCT: dict[Grade, Decimal] = {
    Grade.OUTSTANDING: Decimal("1.00"),
    Grade.GOOD: Decimal("0.80"),
    Grade.PASS: Decimal("0.60"),
    Grade.WARN: Decimal("0"),
    Grade.FAIL: Decimal("0"),
}

# 職稱推薦關鍵字
SUPERVISOR_KEYWORDS: tuple[str, ...] = ("園長", "主任", "執行長", "總園長")
HEAD_TEACHER_KEYWORDS: tuple[str, ...] = ("班導師", "班導")
STAFF_KEYWORDS: tuple[str, ...] = ("行政", "會計", "秘書")
COOK_KEYWORDS: tuple[str, ...] = ("廚工", "廚師", "廚房")
