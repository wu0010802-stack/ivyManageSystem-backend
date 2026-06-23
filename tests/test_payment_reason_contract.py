"""tests/test_payment_reason_contract.py — 契約一致性：PaymentUpdate reason 欄位 description 對齊 MIN_REFUND_REASON_LENGTH。

P3 bug：schemas/activity_admin.py 兩個欄位 description 寫「≥ 5 字」，但 handler 實際
以 MIN_REFUND_REASON_LENGTH=15 驗證，導致前端顯示錯誤提示。本測試確保 description
內的數字與常數同步，防止日後再漂移。
"""

import re

import pytest

from schemas.activity_admin import PaymentUpdate
from utils.activity_constants import MIN_REFUND_REASON_LENGTH


class TestPaymentReasonContract:
    """PaymentUpdate.refund_reason / payment_reason description 須對齊 MIN_REFUND_REASON_LENGTH。"""

    def _extract_min_length_from_description(self, description: str) -> list[int]:
        """從 description 中擷取所有數字（用於比對是否含正確最小長度值）。"""
        return [int(n) for n in re.findall(r"\d+", description)]

    def test_refund_reason_description_contains_correct_min_length(self):
        """refund_reason 的 Field description 應含 MIN_REFUND_REASON_LENGTH 數字，且不含舊值 5（除非 5 == MIN_REFUND_REASON_LENGTH）。"""
        desc = PaymentUpdate.model_fields["refund_reason"].description
        assert desc is not None, "refund_reason 應有 description"
        nums = self._extract_min_length_from_description(desc)
        assert MIN_REFUND_REASON_LENGTH in nums, (
            f"refund_reason description 應含 {MIN_REFUND_REASON_LENGTH}（MIN_REFUND_REASON_LENGTH），"
            f"實際 description：{desc!r}，擷取到的數字：{nums}"
        )
        # 確保舊的錯誤值 5 不再單獨出現（除非恰好等於 MIN_REFUND_REASON_LENGTH）
        if MIN_REFUND_REASON_LENGTH != 5:
            assert 5 not in nums, (
                f"refund_reason description 不應再含舊錯誤值 5，"
                f"實際 description：{desc!r}"
            )

    def test_payment_reason_description_contains_correct_min_length(self):
        """payment_reason 的 Field description 應含 MIN_REFUND_REASON_LENGTH 數字，且不含舊值 5。"""
        desc = PaymentUpdate.model_fields["payment_reason"].description
        assert desc is not None, "payment_reason 應有 description"
        nums = self._extract_min_length_from_description(desc)
        assert MIN_REFUND_REASON_LENGTH in nums, (
            f"payment_reason description 應含 {MIN_REFUND_REASON_LENGTH}（MIN_REFUND_REASON_LENGTH），"
            f"實際 description：{desc!r}，擷取到的數字：{nums}"
        )
        if MIN_REFUND_REASON_LENGTH != 5:
            assert 5 not in nums, (
                f"payment_reason description 不應再含舊錯誤值 5，"
                f"實際 description：{desc!r}"
            )

    def test_both_reason_fields_reference_same_threshold(self):
        """refund_reason 與 payment_reason description 內的字數門檻應一致（同源自同一常數）。"""
        refund_desc = PaymentUpdate.model_fields["refund_reason"].description
        payment_desc = PaymentUpdate.model_fields["payment_reason"].description
        refund_nums = self._extract_min_length_from_description(refund_desc)
        payment_nums = self._extract_min_length_from_description(payment_desc)
        # 兩者都應含相同的 MIN_REFUND_REASON_LENGTH
        assert MIN_REFUND_REASON_LENGTH in refund_nums
        assert MIN_REFUND_REASON_LENGTH in payment_nums
