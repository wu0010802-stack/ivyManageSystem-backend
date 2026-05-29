"""Market intelligence deposit_rate_90d 樣本下限與 sample_size 揭露。

業主決議：visit_90d < SAMPLE_SIZE_THRESHOLD (10) 時，deposit_rate_90d = None；
snapshot row 加 sample_size 欄位讓園長看到統計信心。
"""

import pytest


def test_sample_size_threshold_constant() -> None:
    """SAMPLE_SIZE_THRESHOLD = 10 (GDPR/HIPAA k-anonymity 慣例)"""
    from services.recruitment_market_intelligence import SAMPLE_SIZE_THRESHOLD

    assert SAMPLE_SIZE_THRESHOLD == 10


def test_deposit_rate_below_threshold_returns_none_inline_logic() -> None:
    """模擬 inline 邏輯：visit_90d < 10 → None；>= 10 → float"""
    from services.recruitment_market_intelligence import SAMPLE_SIZE_THRESHOLD

    # 純函式 inline 重現（spec §1893-1897 邏輯）
    def deposit_rate(deposit_90d: int, visit_90d: int):
        return (
            round((deposit_90d / visit_90d) * 100, 1)
            if visit_90d >= SAMPLE_SIZE_THRESHOLD
            else None
        )

    # 樣本不足 — 即使「100% 轉訂率」也回 None 避免誤導
    assert deposit_rate(1, 1) is None
    assert deposit_rate(2, 2) is None
    assert deposit_rate(5, 9) is None  # 99.9% 轉訂率但樣本只 9 → suppress

    # 樣本足夠 — 真實算出比率
    assert deposit_rate(5, 10) == 50.0
    assert deposit_rate(7, 14) == 50.0
    assert deposit_rate(100, 200) == 50.0

    # 樣本足夠 + 0 deposit → 0.0
    assert deposit_rate(0, 10) == 0.0
