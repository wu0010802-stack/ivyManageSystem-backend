"""才藝退費純函式 calculator 測試。

對齊 services/finance/fee_refund_calculator.py 的測試風格：純函式輸入輸出，
不碰 DB。涵蓋 spec §3 規則（三段比例 + T_served=0 特例）+ §10 邊界。
"""

import pytest

from services.activity_refund_calculator import (
    calc_course_refund,
    calc_supply_refund,
)

# ── calc_course_refund: 三段比例 + 特例 ─────────────────────────────────────


def test_course_not_started_refunds_full():
    """T_served=0 特例 → 退 100%，ratio_band='not_started'。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=0)
    assert r["suggested_amount"] == 1500
    assert r["calc_payload"]["ratio_band"] == "not_started"
    assert r["calc_payload"]["refund_ratio"] == "1"
    assert r["warnings"] == []


def test_course_under_one_third_refunds_two_thirds():
    """0 < served_ratio < 1/3 → 退 2/3。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=1)
    # 1500 × 2/3 = 1000
    assert r["suggested_amount"] == 1000
    assert r["calc_payload"]["ratio_band"] == "<1/3"
    assert r["calc_payload"]["refund_ratio"] == "2/3"


def test_course_middle_refunds_one_third():
    """1/3 ≤ served_ratio < 2/3 → 退 1/3。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=4)
    # 1500 × 1/3 = 500
    assert r["suggested_amount"] == 500
    assert r["calc_payload"]["ratio_band"] == "1/3..2/3"
    assert r["calc_payload"]["refund_ratio"] == "1/3"


def test_course_over_two_thirds_no_refund():
    """served_ratio ≥ 2/3 → 0。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=7)
    assert r["suggested_amount"] == 0
    assert r["calc_payload"]["ratio_band"] == ">=2/3"
    assert r["calc_payload"]["refund_ratio"] == "0"


def test_course_exactly_one_third_falls_in_middle():
    """T_served / T_total = 1/3 exactly → 1/3..2/3 段（退 1/3）。"""
    r = calc_course_refund(amount_due=1500, T_total=12, T_served=4)
    assert r["calc_payload"]["ratio_band"] == "1/3..2/3"


def test_course_exactly_two_thirds_no_refund():
    """T_served / T_total = 2/3 exactly → >=2/3 段（不退）。"""
    r = calc_course_refund(amount_due=1500, T_total=12, T_served=8)
    assert r["suggested_amount"] == 0
    assert r["calc_payload"]["ratio_band"] == ">=2/3"


def test_course_T_total_zero_raises():
    """T_total <= 0 → ValueError（對齊 fee_refund_calculator）。"""
    with pytest.raises(ValueError, match="T_total"):
        calc_course_refund(amount_due=1500, T_total=0, T_served=0)


def test_course_T_total_negative_raises():
    """T_total < 0 → ValueError。"""
    with pytest.raises(ValueError, match="T_total"):
        calc_course_refund(amount_due=1500, T_total=-1, T_served=0)


def test_course_T_served_negative_clamps_to_zero():
    """T_served < 0 clamp 0 → 套 not_started 特例全退。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=-5)
    assert r["suggested_amount"] == 1500
    assert r["calc_payload"]["T_served"] == 0
    assert r["calc_payload"]["ratio_band"] == "not_started"


def test_course_T_served_over_total_clamps():
    """T_served > T_total clamp T_total → >=2/3 不退。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=100)
    assert r["suggested_amount"] == 0
    assert r["calc_payload"]["T_served"] == 10


def test_course_round_half_up_applied():
    """1001 × 2/3 = 667.33... → round_half_up → 667。"""
    r = calc_course_refund(amount_due=1001, T_total=10, T_served=1)
    assert r["suggested_amount"] == 667


def test_course_amount_due_zero():
    """amount_due=0 → 各段都 0（避免 div by zero 等假錯）。"""
    r = calc_course_refund(amount_due=0, T_total=10, T_served=0)
    assert r["suggested_amount"] == 0
    r2 = calc_course_refund(amount_due=0, T_total=10, T_served=5)
    assert r2["suggested_amount"] == 0


def test_course_formula_string_contains_key_parts():
    """calc_payload.formula 字串包含 amount/ratio 主要資訊（方便事後查 audit）。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=1)
    f = r["calc_payload"]["formula"]
    assert "1500" in f and "2/3" in f


def test_course_calc_method_label():
    """calc_method 固定字串供 audit 反查。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=1)
    assert r["calc_method"] == "activity_course_ratio"


# ── calc_supply_refund: 用品一律不退 ────────────────────────────────────────


def test_supply_always_zero():
    """用品 suggested 永 0 + warning 提示已交付。"""
    r = calc_supply_refund(amount_due=500)
    assert r["suggested_amount"] == 0
    assert r["calc_method"] == "activity_supply_no_refund"
    assert any("交付" in w or "不予退費" in w for w in r["warnings"])


def test_supply_amount_due_zero():
    """amount_due=0 也回 0 + warning，不報錯。"""
    r = calc_supply_refund(amount_due=0)
    assert r["suggested_amount"] == 0
    assert r["warnings"]
