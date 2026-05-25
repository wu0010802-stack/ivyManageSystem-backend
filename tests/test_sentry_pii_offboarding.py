"""tests/test_sentry_pii_offboarding.py — 驗證 offboarding 新 PII 欄位被 denylist 涵蓋。"""

from utils.sentry_init import _scrub_event


def test_resign_reason_is_scrubbed():
    """resign_reason 包含員工離職原因，屬個資，應被遮罩。"""
    event = {"extra": {"resign_reason": "個人因素"}}
    scrubbed = _scrub_event(event, None)
    assert scrubbed["extra"]["resign_reason"] != "個人因素"
    assert scrubbed["extra"]["resign_reason"] == "[Filtered]"


def test_leave_balance_snapshot_is_scrubbed():
    """leave_balance_snapshot 包含特休/病假等敏感資訊，應被遮罩。"""
    event = {
        "extra": {"leave_balance_snapshot": {"daily_wage": 1800, "annual_hours": 80}}
    }
    scrubbed = _scrub_event(event, None)
    assert scrubbed["extra"]["leave_balance_snapshot"] != {
        "daily_wage": 1800,
        "annual_hours": 80,
    }
    assert scrubbed["extra"]["leave_balance_snapshot"] == "[Filtered]"


def test_certificate_pdf_path_is_scrubbed():
    """certificate_pdf_path 雖為檔路徑，但路徑含員工姓名/ID，應被遮罩。"""
    event = {"extra": {"certificate_pdf_path": "storage/offboarding/42_2026-06-15.pdf"}}
    scrubbed = _scrub_event(event, None)
    # 驗證不含原始路徑
    assert "42_2026-06-15.pdf" not in str(scrubbed["extra"]["certificate_pdf_path"])
    assert scrubbed["extra"]["certificate_pdf_path"] == "[Filtered]"
