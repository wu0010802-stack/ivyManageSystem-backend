"""退費計算純函式測試"""

from datetime import date

import pytest

from services.fee_refund_calculator import (
    calc_enrollment_refund,
    calc_monthly_refund,
    longest_consecutive_workdays,
)

# --------- calc_enrollment_refund: 註冊費/雜費 三段比例 ---------


def test_enrollment_under_one_third_refunds_two_thirds():
    """服務日 < 1/3 → 退 2/3。"""
    r = calc_enrollment_refund(amount_due=19000, T_total=100, T_served=30)
    assert r["suggested_amount"] == round(19000 * 2 / 3)
    assert r["calc_payload"]["ratio"] == "<1/3"
    assert r["calc_payload"]["refund_ratio"] == "2/3"


def test_enrollment_middle_refunds_one_third():
    """1/3 ≤ 服務日 < 2/3 → 退 1/3。"""
    r = calc_enrollment_refund(amount_due=19000, T_total=100, T_served=50)
    assert r["suggested_amount"] == round(19000 * 1 / 3)
    assert r["calc_payload"]["ratio"] == "1/3..2/3"
    assert r["calc_payload"]["refund_ratio"] == "1/3"


def test_enrollment_over_two_thirds_no_refund():
    """服務日 ≥ 2/3 → 不退。"""
    r = calc_enrollment_refund(amount_due=19000, T_total=100, T_served=70)
    assert r["suggested_amount"] == 0
    assert r["calc_payload"]["ratio"] == ">=2/3"
    assert r["calc_payload"]["refund_ratio"] == "0"


def test_enrollment_boundary_exactly_one_third():
    """T_served = T_total/3 邊界 → 落在 1/3..2/3 區段(退 1/3)。"""
    r = calc_enrollment_refund(amount_due=19000, T_total=99, T_served=33)
    assert r["calc_payload"]["refund_ratio"] == "1/3"


def test_enrollment_boundary_exactly_two_thirds():
    """T_served = 2T_total/3 邊界 → 落在 >=2/3 區段(不退)。"""
    r = calc_enrollment_refund(amount_due=19000, T_total=99, T_served=66)
    assert r["suggested_amount"] == 0


def test_enrollment_T_total_zero_raises():
    """T_total=0 → 拒絕(避免除零)。"""
    with pytest.raises(ValueError, match="T_total"):
        calc_enrollment_refund(amount_due=19000, T_total=0, T_served=0)


# --------- longest_consecutive_workdays ---------


def test_consecutive_workdays_simple():
    """單一連續區間 5 個工作日(週一~週五)。"""
    holiday_map, makeup_map = {}, {}
    # 2026-05-04 是週一
    days = [date(2026, 5, 4 + i) for i in range(5)]  # 5/4-5/8 週一到週五
    n = longest_consecutive_workdays(days, holiday_map, makeup_map)
    assert n == 5


def test_consecutive_workdays_excludes_weekend():
    """請假涵蓋週末 → 週末不算上課日,但仍視為連續區段。"""
    # 5/1(五) + 5/2(六) + 5/3(日) + 5/4(一) → 工作日 5/1+5/4 = 2 連續
    days = [date(2026, 5, d) for d in [1, 2, 3, 4]]
    n = longest_consecutive_workdays(days, {}, {})
    assert n == 2


def test_consecutive_workdays_break_on_gap():
    """5/4-5/5 中斷 5/6 不請假 5/7-5/8 → 最長 = 2。"""
    days = [date(2026, 5, d) for d in [4, 5, 7, 8]]  # 5/4-5(週一二) + 5/7-8(週四五)
    n = longest_consecutive_workdays(days, {}, {})
    assert n == 2


def test_consecutive_workdays_holiday_treated_as_continuation():
    """國定假日在中間 → 不破壞連續。"""
    holiday_map = {date(2026, 5, 5): "勞動節"}  # 假設 5/5 是假日
    days = [date(2026, 5, d) for d in [4, 5, 6, 7, 8]]
    n = longest_consecutive_workdays(days, holiday_map, {})
    # 5/4 工作 + 5/5 假日(不算) + 5/6 工作 + 5/7 工作 + 5/8 工作 = 連續 4 工作日
    assert n == 4


# --------- calc_monthly_refund ---------


def test_monthly_consecutive_lt_5_no_refund():
    """連續 < 5 個上課日 → 不退。"""
    r = calc_monthly_refund(
        amount_due=13000,
        breakdown={"tuition": 8500, "meal": 3000, "transport": 1500},
        L_consecutive=4,
        work_days_in_month=20,
        advance_filed=True,
    )
    assert r["suggested_amount"] == 0
    assert "未達 5 個" in r["warnings"][0]


def test_monthly_not_advance_filed_no_refund():
    """未事先請假 → 不退。"""
    r = calc_monthly_refund(
        amount_due=13000,
        breakdown={"tuition": 8500, "meal": 3000, "transport": 1500},
        L_consecutive=10,
        work_days_in_month=20,
        advance_filed=False,
    )
    assert r["suggested_amount"] == 0
    assert any("事先" in w for w in r["warnings"])


def test_monthly_refunds_meal_plus_transport_proportion():
    """連續 10 上課日 / 20 工作日 = 1/2,退 (3000+1500)/2 = 2250。"""
    r = calc_monthly_refund(
        amount_due=13000,
        breakdown={"tuition": 8500, "meal": 3000, "transport": 1500},
        L_consecutive=10,
        work_days_in_month=20,
        advance_filed=True,
    )
    assert r["suggested_amount"] == round((3000 + 1500) * 10 / 20)
    assert r["calc_payload"]["L_consecutive"] == 10
    assert r["calc_payload"]["refundable_components"] == ["meal", "transport"]


def test_monthly_no_breakdown_fallback_full_amount_proportion():
    """無 breakdown → 全額按比例退 + 警告。"""
    r = calc_monthly_refund(
        amount_due=13000,
        breakdown=None,
        L_consecutive=10,
        work_days_in_month=20,
        advance_filed=True,
    )
    assert r["suggested_amount"] == round(13000 * 10 / 20)
    assert any("無 breakdown" in w for w in r["warnings"])


def test_monthly_breakdown_missing_meal_transport_zero():
    """breakdown 沒餐點/交通 → 退 0。"""
    r = calc_monthly_refund(
        amount_due=13000,
        breakdown={"tuition": 13000},
        L_consecutive=10,
        work_days_in_month=20,
        advance_filed=True,
    )
    assert r["suggested_amount"] == 0
