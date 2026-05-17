"""tests/test_activity_constants.py — utils/activity_constants.py 常數 sanity test。

純列舉常數，主要驗證：
1. 型別與正負號（避免 typo 變號）
2. 數值合理範圍（避免 99_999 vs 999_999 之類 typo）
3. 常數彼此關係（refund threshold < item high price）
"""

from utils import activity_constants as ac


class TestPaymentAmountConstant:
    def test_max_payment_amount_is_999_999(self):
        # F2 第三階段曾因 typo 99_999 vs 999_999 引入 regression
        assert ac.MAX_PAYMENT_AMOUNT == 999_999

    def test_max_payment_amount_is_positive_int(self):
        assert isinstance(ac.MAX_PAYMENT_AMOUNT, int)
        assert ac.MAX_PAYMENT_AMOUNT > 0


class TestRefundConstants:
    def test_min_refund_reason_length(self):
        assert ac.MIN_REFUND_REASON_LENGTH == 15

    def test_refund_threshold_positive(self):
        assert ac.REFUND_APPROVAL_THRESHOLD == 1000
        assert isinstance(ac.REFUND_APPROVAL_THRESHOLD, int)

    def test_refund_threshold_less_than_high_price(self):
        # 退費門檻應該低於課程高價門檻（否則邏輯倒置）
        assert ac.REFUND_APPROVAL_THRESHOLD < ac.ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD


class TestVoidConstants:
    def test_min_void_reason_length(self):
        assert ac.MIN_VOID_REASON_LENGTH == 5

    def test_void_reason_shorter_than_refund(self):
        # 註解：「嚴於 Void 是因退費直接影響財務流水」
        assert ac.MIN_VOID_REASON_LENGTH < ac.MIN_REFUND_REASON_LENGTH


class TestPaymentDateBackLimit:
    def test_default_30_days(self):
        assert ac.PAYMENT_DATE_BACK_LIMIT_DAYS == 30

    def test_is_positive_int(self):
        assert isinstance(ac.PAYMENT_DATE_BACK_LIMIT_DAYS, int)
        assert ac.PAYMENT_DATE_BACK_LIMIT_DAYS > 0


class TestActivityItemHighPriceThreshold:
    def test_value_30_000(self):
        assert ac.ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD == 30_000

    def test_less_than_max_payment(self):
        # 高價門檻應該低於單筆上限（否則永遠不會觸發審批）
        assert ac.ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD < ac.MAX_PAYMENT_AMOUNT

    def test_is_positive_int(self):
        assert isinstance(ac.ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD, int)
        assert ac.ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD > 0
