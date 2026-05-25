"""Unit tests for utils/rounding.py

涵蓋：
- .5 邊界（HALF_EVEN vs HALF_UP 差異）
- 負數行為（HALF_UP 對負數定義：away from zero）
- ndigits 參數行為（與 builtin round 對比）
- None / int / float / Decimal 三型別輸入
- IEEE 殘渣防護（0.1 + 0.2 = 0.30000000000000004 等）
"""

from decimal import Decimal

import pytest

from utils.rounding import round_half_up


class TestHalfBoundary:
    """0.5 邊界 — HALF_EVEN（builtin round）vs HALF_UP 差異的核心案例。"""

    @pytest.mark.parametrize(
        "value,expected_half_up,builtin_half_even",
        [
            (0.5, 1, 0),
            (1.5, 2, 2),
            (2.5, 3, 2),
            (3.5, 4, 4),
            (4.5, 5, 4),
            (442.5, 443, 442),
            (752.5, 753, 752),
        ],
    )
    def test_positive_half_boundary(self, value, expected_half_up, builtin_half_even):
        # 證實 helper 與 builtin round 在 .5 邊界 50% 翻盤
        assert round_half_up(value) == expected_half_up
        assert round(value) == builtin_half_even

    @pytest.mark.parametrize(
        "value,expected",
        [
            (-0.5, -1),  # HALF_UP 對負數：away from zero
            (-1.5, -2),
            (-2.5, -3),
            (-442.5, -443),
        ],
    )
    def test_negative_half_boundary(self, value, expected):
        assert round_half_up(value) == expected


class TestNonHalfBoundary:
    """非 .5 邊界 — helper 與 builtin round 必須一致。"""

    @pytest.mark.parametrize(
        "value,expected",
        [
            (0.4, 0),
            (0.6, 1),
            (442.4, 442),
            (442.6, 443),
            (-442.4, -442),
            (-442.6, -443),
        ],
    )
    def test_consistent_with_builtin(self, value, expected):
        assert round_half_up(value) == expected
        assert round(value) == expected


class TestNdigits:
    """ndigits 參數 — 對齊 builtin round 簽章。"""

    def test_two_decimals_basic(self):
        assert round_half_up(0.125, 2) == 0.13  # HALF_UP（builtin 為 0.12）
        assert round_half_up(0.135, 2) == 0.14
        assert round_half_up(0.115, 2) == 0.12

    def test_returns_float_when_ndigits_positive(self):
        result = round_half_up(1.5, 1)
        assert isinstance(result, float)
        assert result == 1.5

    def test_returns_int_when_ndigits_zero(self):
        result = round_half_up(1.5)
        assert isinstance(result, int)
        assert result == 2


class TestInputTypes:
    """確認 int / float / Decimal / None 都接受。"""

    def test_int_input(self):
        assert round_half_up(5) == 5

    def test_float_input(self):
        assert round_half_up(5.5) == 6

    def test_decimal_input(self):
        assert round_half_up(Decimal("5.5")) == 6

    def test_none_input_returns_zero(self):
        assert round_half_up(None) == 0
        assert round_half_up(None, 2) == 0.0


class TestIEEEResidueGuard:
    """str(value) 進 Decimal 避免 IEEE 浮點殘渣。

    Decimal(0.1) = Decimal('0.10000000000000000555...')
    Decimal('0.1') = Decimal('0.1')   ← 我們要這個

    audit Layer 2 證實「真正 float 殘渣產生 .5 邊界 drift 的場景幾乎不出現」
    （因為 hours/8 和 daily_salary 乘上 ratio 後是有限二進位小數），
    但 helper 仍須對未來可能引入殘渣的呼叫 robust。
    """

    def test_ieee_dirty_value_quantizes_clean(self):
        # 0.1 + 0.2 = 0.30000000000000004 (IEEE)
        dirty = 0.1 + 0.2
        # 不能寫 round_half_up(0.3) 否則沒測到殘渣
        # 此處 dirty == 0.30000000000000004，量化到 1 位 → 0.3
        assert round_half_up(dirty, 1) == 0.3

    def test_large_dirty_sum_still_correct(self):
        # 模擬 audit Layer 2 的 sum scenario
        # 442.5 + IEEE 殘渣（不影響 .5 邊界判定）
        values = [125.0 * 2 * 1.34, 125.0 * 2 * 1.67]  # 335.0 + 417.5
        total = sum(values)
        assert round_half_up(total) == 753  # vs builtin round = 752
