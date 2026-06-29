import pytest
from datetime import date
from sqlalchemy.exc import IntegrityError
from models.misc_receipt import MiscReceipt, RECEIPT_CATEGORIES


def test_misc_receipt_amount_must_be_positive(test_db_session):
    row = MiscReceipt(
        receipt_date=date(2026, 6, 1),
        payer_name="某基金會",
        category="donation",
        amount=0,
        payment_method="cash",
        status="pending",
        attachments=[],
    )
    test_db_session.add(row)
    with pytest.raises(IntegrityError):
        test_db_session.flush()


def test_misc_receipt_categories_constant():
    assert set(RECEIPT_CATEGORIES) == {
        "rent",
        "donation",
        "subsidy",
        "secondhand_sale",
        "refund_recovery",
        "other",
    }
