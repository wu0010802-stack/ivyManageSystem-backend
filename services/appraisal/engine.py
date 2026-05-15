"""半年考核計算引擎（M2）— 5-step 純函式設計，無 DB 依賴。

對應 Excel「114(上)年度考核統計表」的計算邏輯：

  step1 base_score    = round(actual_enrollment / enrollment_target * 100, 1)
  step2 event_sum     = Σ score_items.score_delta
  step3 total_score   = clamp(base + sum, 0, 110)
  step4 grade         = OUTSTANDING/GOOD/PASS/WARN/FAIL（優/甲/乙/丙/丁）
  step5 bonus_amount  = base_amount × (total_score / 100)，base 由 (role_group, grade,
                        effective_from) 查 appraisal_bonus_rates；PASS/WARN/FAIL 為 0

設計原則：
- 全程使用 Decimal 避免浮點誤差（quantize 到小數第 2 位）
- 純函式：輸入 dataclass / 原始值，回傳 SummaryComputed dataclass
- DB session 由 caller 處理；本模組不接觸 ORM
- 邊界 case 由 caller 控制：is_excluded participant 不應呼叫 engine
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional

from models.appraisal import Grade, RoleGroup

logger = logging.getLogger(__name__)

# Excel 半年週期最大月數（plan §5 邊界 case：到職未滿半學期按比例底數）
HALF_YEAR_MONTHS = Decimal("6")

# 等第切點 — Excel 規則：優 ≥ 90、甲 80-89、乙 70-79、丙 60-69、丁 < 60
_GRADE_THRESHOLDS: tuple[tuple[Decimal, Grade], ...] = (
    (Decimal("90"), Grade.OUTSTANDING),
    (Decimal("80"), Grade.GOOD),
    (Decimal("70"), Grade.PASS),
    (Decimal("60"), Grade.WARN),
)

# Total score 上下限（避免異常 score_items 導致負分或破百）
TOTAL_SCORE_MIN = Decimal("0")
TOTAL_SCORE_MAX = Decimal("110")

# 不發獎金的等第
_NO_BONUS_GRADES: frozenset[Grade] = frozenset({Grade.PASS, Grade.WARN, Grade.FAIL})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BonusRateLookup:
    """從 DB 載入後的獎金率查表（純資料；caller 自行從 appraisal_bonus_rates 撈）。

    用 dict 而非 ORM 是為了讓 engine 可單獨單元測試。
    key: (effective_from_iso, role_group, grade)
    value: base_amount（Decimal）
    """

    rates: dict[tuple[str, RoleGroup, Grade], Decimal]

    def resolve(
        self, on_date: date, role_group: RoleGroup, grade: Grade
    ) -> Optional[Decimal]:
        """找出 on_date 當下適用的 base_amount；無對應 rate 回傳 None。

        規則：取最大 effective_from 且 ≤ on_date 的那筆。
        """
        candidates = [
            (effective_iso, amount)
            for (effective_iso, rg, gr), amount in self.rates.items()
            if rg == role_group
            and gr == grade
            and effective_iso <= on_date.isoformat()
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]


@dataclass(frozen=True)
class SummaryComputed:
    """5-step 計算結果；caller 用於 upsert appraisal_summaries。"""

    base_score: Decimal
    event_score_sum: Decimal
    total_score: Decimal
    grade: Grade
    bonus_amount: Decimal


# ---------------------------------------------------------------------------
# Step 1: 基礎分數
# ---------------------------------------------------------------------------


def compute_base_score(
    actual_enrollment: int, enrollment_target: int
) -> Decimal:
    """step1: base_score = actual / target × 100，保留 1 位小數。

    對應 Excel「9/15 分數 = 121/160 = 75.6%」。
    enrollment_target == 0 視為設定錯誤，回傳 0 並記 warning。
    """
    if enrollment_target <= 0:
        logger.warning(
            "compute_base_score: enrollment_target %s 不合法，回傳 0",
            enrollment_target,
        )
        return Decimal("0.0")
    if actual_enrollment < 0:
        actual_enrollment = 0
    raw = Decimal(actual_enrollment) / Decimal(enrollment_target) * Decimal("100")
    return raw.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Step 2: 加減分加總
# ---------------------------------------------------------------------------


def sum_score_items(deltas: Iterable[Decimal | float | int | None]) -> Decimal:
    """step2: event_score_sum = Σ deltas，保留 2 位小數。

    None 或非數字視為 0（防呆）。
    """
    total = Decimal("0")
    for d in deltas:
        if d is None:
            continue
        try:
            total += Decimal(str(d))
        except Exception:
            logger.warning("sum_score_items: 略過不可解析的 delta %r", d)
            continue
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Step 3: 總分
# ---------------------------------------------------------------------------


def compute_total_score(base_score: Decimal, event_score_sum: Decimal) -> Decimal:
    """step3: total = clamp(base + sum, 0, 110)。保留 2 位小數。"""
    raw = (Decimal(base_score) + Decimal(event_score_sum)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if raw < TOTAL_SCORE_MIN:
        return TOTAL_SCORE_MIN
    if raw > TOTAL_SCORE_MAX:
        return TOTAL_SCORE_MAX
    return raw


# ---------------------------------------------------------------------------
# Step 4: 等第
# ---------------------------------------------------------------------------


def classify_grade(total_score: Decimal) -> Grade:
    """step4: 依切點分等第。"""
    score = Decimal(total_score)
    for threshold, grade in _GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return Grade.FAIL


# ---------------------------------------------------------------------------
# Step 5: 獎金
# ---------------------------------------------------------------------------


def compute_bonus_amount(
    total_score: Decimal,
    grade: Grade,
    role_group: RoleGroup,
    bonus_rates: BonusRateLookup,
    on_date: date,
) -> Decimal:
    """step5: bonus = base × (total / 100)；PASS/WARN/FAIL 無獎金。

    base 從 bonus_rates 查 (role_group, grade, effective_from ≤ on_date) 的最大那筆。
    若 base = 0 或查無對應 rate，bonus_amount = 0。
    """
    if grade in _NO_BONUS_GRADES:
        return Decimal("0.00")
    base = bonus_rates.resolve(on_date, role_group, grade)
    if base is None or base <= 0:
        logger.warning(
            "compute_bonus_amount: 無對應獎金率（date=%s role=%s grade=%s），bonus=0",
            on_date,
            role_group,
            grade,
        )
        return Decimal("0.00")
    raw = Decimal(base) * Decimal(total_score) / Decimal("100")
    return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# 高階：整合 5 step 為單一呼叫
# ---------------------------------------------------------------------------


def compute_summary(
    actual_enrollment: int,
    enrollment_target: int,
    score_deltas: Iterable[Decimal | float | int | None],
    role_group: RoleGroup,
    bonus_rates: BonusRateLookup,
    on_date: date,
) -> SummaryComputed:
    """便利函式：一次跑完 5 step 計算。"""
    base_score = compute_base_score(actual_enrollment, enrollment_target)
    event_score_sum = sum_score_items(score_deltas)
    total_score = compute_total_score(base_score, event_score_sum)
    grade = classify_grade(total_score)
    bonus_amount = compute_bonus_amount(
        total_score, grade, role_group, bonus_rates, on_date
    )
    return SummaryComputed(
        base_score=base_score,
        event_score_sum=event_score_sum,
        total_score=total_score,
        grade=grade,
        bonus_amount=bonus_amount,
    )


# ---------------------------------------------------------------------------
# 額外：到職未滿一年的比例底數（plan §5 邊界 case 1）
# ---------------------------------------------------------------------------


def proration_rate(hire_months_in_cycle: Decimal) -> Decimal:
    """到職月數比例：hire_months / 6（半年週期）。clamp 0~1。

    用於到職未滿半學期的人，bonus_amount 額外乘此比例。
    """
    rate = (Decimal(hire_months_in_cycle) / HALF_YEAR_MONTHS).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    if rate < Decimal("0"):
        return Decimal("0")
    if rate > Decimal("1"):
        return Decimal("1")
    return rate
