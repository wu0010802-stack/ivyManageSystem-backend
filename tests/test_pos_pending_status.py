"""POS 未簽核日狀態應為 pending 而非 rejected（2026-06-15 運作探測 P3-1）。

Bug：_live_preview 與 reconciliation fallback 對未簽核日（is_approved=False）
  硬寫 ApprovalStatus.REJECTED，但 POS 模組無 reject 動作（有 daily-close 列＝
  approved，無列＝未日結，僅兩態）。rejected 為錯誤語意，污染 API 契約/匯出/
  前端 codegen。
"""

import datetime as dt

import api.activity.pos_approval as pos_approval
from models.approval import ApprovalStatus


def test_live_preview_unapproved_day_is_pending(monkeypatch):
    monkeypatch.setattr(
        pos_approval,
        "compute_daily_snapshot",
        lambda session, target_date: {
            "payment_total": 0,
            "refund_total": 0,
            "net": 0,
            "transaction_count": 0,
            "by_method_net": {},
        },
    )
    result = pos_approval._live_preview(None, dt.date(2025, 10, 1))
    assert result["is_approved"] is False
    assert result["status"] == ApprovalStatus.PENDING.value
    assert result["status"] != ApprovalStatus.REJECTED.value
