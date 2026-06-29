"""雜項收款納入財報收入聚合 — TDD 測試（Task 7）。

驗證 get_misc_receipt_revenue_by_month：
- 按 receipt_date 月份正確彙總金額
- pending 與 signed 兩種 status 均計入（與廠商付款支出口徑對齊）
"""

from datetime import date

from models.misc_receipt import MiscReceipt
from services.finance_report_service import get_misc_receipt_revenue_by_month


def test_misc_revenue_aggregates_by_month_including_pending(test_db_session):
    test_db_session.add_all(
        [
            MiscReceipt(
                receipt_date=date(2026, 3, 5),
                payer_name="A",
                category="rent",
                amount=1000,
                payment_method="cash",
                status="pending",
                attachments=[],
            ),
            MiscReceipt(
                receipt_date=date(2026, 3, 20),
                payer_name="B",
                category="donation",
                amount=500,
                payment_method="cash",
                status="signed",
                attachments=[],
            ),
            MiscReceipt(
                receipt_date=date(2026, 4, 1),
                payer_name="C",
                category="other",
                amount=200,
                payment_method="cash",
                status="pending",
                attachments=[],
            ),
        ]
    )
    test_db_session.flush()
    result = get_misc_receipt_revenue_by_month(test_db_session, 2026)
    assert result.get(3) == 1500  # pending + signed 都計入
    assert result.get(4) == 200


def test_misc_revenue_excludes_other_year(test_db_session):
    """跨年資料不應計入當年彙總。"""
    test_db_session.add_all(
        [
            MiscReceipt(
                receipt_date=date(2025, 12, 31),
                payer_name="X",
                category="rent",
                amount=9999,
                payment_method="cash",
                status="signed",
                attachments=[],
            ),
            MiscReceipt(
                receipt_date=date(2026, 1, 1),
                payer_name="Y",
                category="donation",
                amount=300,
                payment_method="cash",
                status="pending",
                attachments=[],
            ),
        ]
    )
    test_db_session.flush()
    result = get_misc_receipt_revenue_by_month(test_db_session, 2026)
    # 2025 的那筆不應出現；2026-01 的 300 應有
    assert 12 not in result  # 去年 12 月不在 2026 彙總內
    assert result.get(1) == 300
