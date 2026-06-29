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


def test_misc_receipt_permissions_exist():
    from utils.permissions import Permission, PERMISSION_LABELS

    assert Permission.MISC_RECEIPT_READ.value == "MISC_RECEIPT_READ"
    assert Permission.MISC_RECEIPT_WRITE.value == "MISC_RECEIPT_WRITE"
    assert PERMISSION_LABELS["MISC_RECEIPT_READ"] == "雜項收款 (檢視)"
    assert PERMISSION_LABELS["MISC_RECEIPT_WRITE"] == "雜項收款 (編輯/簽收)"


def test_misc_receipt_in_finance_roles():
    from utils.permissions import ROLE_TEMPLATES

    for role in ("hr", "supervisor", "accountant"):
        assert "MISC_RECEIPT_READ" in ROLE_TEMPLATES[role]
        assert "MISC_RECEIPT_WRITE" in ROLE_TEMPLATES[role]
